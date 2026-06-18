"""
Phase 2.5 unit tests — confirmed wellness outcomes.

Covers all four outcome states (confirmed_ok / answered_unconfirmed / missed /
failed) at three layers: the 3CX provider's classification of voice-app call
status, the mock provider, and the scheduler's pass/retry/escalation behavior.

Run:  cd ~/projects/guardian && PYTHONPATH=. python3 tests/test_outcomes.py
(Also importable by pytest. No live calls — fully mocked.)
"""
import io
import json
import os
import tempfile
import urllib.request

# Configure a throwaway env BEFORE importing app (config.env reads os.environ first).
os.environ["GUARDIAN_DB"] = tempfile.mktemp(suffix=".db")
os.environ["GUARDIAN_RETRY_MINUTES"] = "20"
os.environ["GUARDIAN_MAX_ATTEMPTS"] = "3"
os.environ["CALL_PROVIDER"] = "mock"
os.environ["GUARDIAN_RING_SECONDS"] = "1"
os.environ["GUARDIAN_CONFIRM"] = "true"
os.environ["TELEGRAM_BOT_TOKEN"] = ""   # silence Telegram in tests
os.environ["TELEGRAM_CHAT_ID"] = ""

from app import call_provider as cp        # noqa: E402
from app import scheduler, storage         # noqa: E402
from app.models import CheckinStatus       # noqa: E402


# ---- helpers ----
class _FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _mock_voiceapp(status_data, captured=None):
    """Patch urllib so POST returns a callId and GET returns canned call status.
    If `captured` (dict) is passed, the POST request body is stored under 'body'."""
    def _open(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/outbound-call"):
            if captured is not None and not isinstance(req, str):
                captured["body"] = json.loads(req.data.decode())
            return _FakeResp(json.dumps({"success": True, "callId": "t"}).encode())
        return _FakeResp(json.dumps({"success": True, "data": status_data}).encode())
    urllib.request.urlopen = _open


# ---- 1. provider classification (voice-app status -> outcome) ----
def test_provider_classification():
    # ANGEL-05 two-choice menu. The voice-app reports the pressed key as "confirmDigit"
    # (and confirmed=true when it equals the okay digit). 1 = okay, 2 = needs Darcee.
    cases = {
        "press_1_okay":   ({"state": "completed", "answeredAt": "x", "confirmed": True, "confirmDigit": "1"}, "confirmed_ok"),
        "press_2_darcee": ({"state": "completed", "answeredAt": "x", "confirmed": False, "confirmDigit": "2"}, "needs_darcee"),
        "legacy_confirm": ({"state": "completed", "answeredAt": "x", "confirmed": True}, "confirmed_ok"),
        "no_press":       ({"state": "completed", "answeredAt": "x", "confirmed": False, "confirmDigit": None}, "answered_unconfirmed"),
        "no_answer":      ({"state": "failed", "answeredAt": None, "failureReason": "no_answer"}, "missed"),
        "busy":           ({"state": "failed", "answeredAt": None, "failureReason": "busy"}, "missed"),
        "tech_503":       ({"state": "failed", "answeredAt": None, "failureReason": "service_unavailable"}, "failed"),
        "tech_404":       ({"state": "failed", "answeredAt": None, "failureReason": "not_found"}, "failed"),
    }
    for name, (data, expected) in cases.items():
        _mock_voiceapp(data)
        res = cp.ThreeCXProvider().place_call("test", "39510")
        assert res.status == expected, f"{name}: got {res.status}, expected {expected}"


# ---- 1b. provider sends the menu contract + audible acknowledgments ----
def test_provider_payload_has_confirm_ack():
    captured = {}
    _mock_voiceapp({"state": "completed", "answeredAt": "x", "confirmed": True, "confirmDigit": "1"}, captured)
    cp.ThreeCXProvider().place_call("test", "39510")
    b = captured["body"]
    assert b["mode"] == "announce", b.get("mode")
    assert b["acceptDigits"] == ["1", "2"], b.get("acceptDigits")
    ack = b.get("confirmAck") or {}
    assert ack.get("1") == "Thank you, Mom. I'm glad you're okay. Have a wonderful day.", ack
    assert ack.get("2") == "Thank you, Mom. I'll let Darcee know you'd like a call. Talk to you later.", ack


# ---- 2. mock provider returns all five states ----
def test_mock_provider_states():
    for want, expected in [("confirmed_ok", "confirmed_ok"),
                           ("needs_darcee", "needs_darcee"),
                           ("answered_unconfirmed", "answered_unconfirmed"),
                           ("missed", "missed"),
                           ("failed", "failed"),
                           ("answered", "confirmed_ok")]:  # legacy alias
        cp.set_mock_result(want)
        res = cp.MockProvider().place_call("test", "x")
        cp.set_mock_result(None)
        assert res.status == expected, f"mock {want}: got {res.status}"


# ---- 3. scheduler: only confirmed_ok passes ----
def test_scheduler_confirmed_ok_passes():
    storage.init_db()
    ci = scheduler.trigger_mock_check(result="confirmed_ok")
    assert ci["final_status"] == CheckinStatus.ANSWERED, ci["final_status"]
    assert ci["answered_attempt_number"] == 1
    assert ci["wellness_result"] == "okay", ci["wellness_result"]


def test_scheduler_needs_darcee_is_terminal_not_failure():
    """Pressing 2 = a COMPLETED check that pings Darcee — no retry, no escalation."""
    storage.init_db()
    ci = scheduler.trigger_mock_check(result="needs_darcee")
    assert ci["final_status"] == CheckinStatus.NEEDS_DARCEE, ci["final_status"]
    assert ci["wellness_result"] == "needs_call", ci["wellness_result"]
    assert not ci["next_attempt_at"], "needs_darcee must NOT schedule a retry"
    assert ci["escalation_sent"] == 0, "needs_darcee is not an escalation"
    assert ci["answered_attempt_number"] == 1


def test_scheduler_unconfirmed_is_not_okay():
    storage.init_db()
    ci = scheduler.trigger_mock_check(result="answered_unconfirmed")
    # connected but not confirmed -> NOT a pass; retry scheduled (max_attempts=3)
    assert ci["final_status"] == CheckinStatus.PENDING, ci["final_status"]
    assert ci["next_attempt_at"], "expected a retry to be scheduled"


def test_scheduler_missed_and_failed_retry():
    storage.init_db()
    for result in ("missed", "failed"):
        ci = scheduler.trigger_mock_check(result=result)
        assert ci["final_status"] == CheckinStatus.PENDING, f"{result}: {ci['final_status']}"
        assert ci["next_attempt_at"], f"{result}: expected retry"


def test_scheduler_escalates_unconfirmed_on_last_attempt():
    """On the final attempt, an unconfirmed call must escalate (not pass)."""
    storage.init_db()
    cid = storage.create_checkin(scheduler._now().isoformat())
    # pretend the first two attempts already happened -> next is the last (max=3)
    storage.update_checkin(cid, attempt_count=2)
    cp.set_mock_result("answered_unconfirmed")
    scheduler._run_attempt(storage.get_checkin(cid))
    cp.set_mock_result(None)
    ci = storage.get_checkin(cid)
    assert ci["final_status"] == CheckinStatus.ESCALATED, ci["final_status"]
    assert ci["escalation_sent"] == 1


# ---- ANGEL-06: call-back acknowledgment + reminders ----
def test_needs_darcee_is_unacked_then_acknowledged():
    storage.init_db()
    ci = scheduler.trigger_mock_check(result="needs_darcee")
    pending = storage.unacked_needs_darcee()
    assert any(p["id"] == ci["id"] for p in pending), "needs_darcee should start un-acknowledged"
    upd = storage.acknowledge_checkin(ci["id"])
    assert upd["acknowledged"] == 1 and upd["acknowledged_at"], upd
    assert not any(p["id"] == ci["id"] for p in storage.unacked_needs_darcee()), "ack must remove it from pending"


def test_acknowledge_and_confirm_sends_once():
    storage.init_db()
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a[0] if a else ""), (True, "sent"))[1]
    try:
        ci = scheduler.trigger_mock_check(result="needs_darcee")
        sent.clear()
        out, changed = scheduler.acknowledge_and_confirm(ci["id"], by="test")
        assert changed and out["acknowledged"] == 1, out
        assert any("called Mom back" in str(m) for m in sent), "Angel should confirm the acknowledgment"
        # second call must NOT re-confirm
        sent.clear()
        out2, changed2 = scheduler.acknowledge_and_confirm(ci["id"], by="test")
        assert changed2 is False and len(sent) == 0, "no duplicate confirmation on re-ack"
    finally:
        scheduler.telegram_notify.send = orig


def test_start_date_gate():
    import os as _os
    from datetime import timedelta
    try:
        future = (scheduler._now() + timedelta(days=1)).date().isoformat()
        past = (scheduler._now() - timedelta(days=1)).date().isoformat()
        _os.environ["GUARDIAN_START_DATE"] = future
        assert scheduler._active() is False, "must be inactive before go-live date"
        assert scheduler.next_scheduled_check() is not None, "next check should still resolve (future day)"
        _os.environ["GUARDIAN_START_DATE"] = past
        assert scheduler._active() is True, "active on/after go-live date"
    finally:
        _os.environ.pop("GUARDIAN_START_DATE", None)


def test_quiet_hours_window():
    from datetime import datetime
    tz = scheduler._tz()
    # default quiet 21:00–08:00
    assert scheduler._in_quiet_hours(datetime(2026, 6, 18, 23, 0, tzinfo=tz)) is True
    assert scheduler._in_quiet_hours(datetime(2026, 6, 18, 3, 0, tzinfo=tz)) is True
    assert scheduler._in_quiet_hours(datetime(2026, 6, 18, 12, 0, tzinfo=tz)) is False


def test_ack_reminder_fires_when_due_and_stops_after_ack(monkeypatchless=True):
    storage.init_db()
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a), (True, "sent"))[1]
    try:
        ci = scheduler.trigger_mock_check(result="needs_darcee")
        sent.clear()
        # backdate created + last_reminder so a reminder is due, and force daytime
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        storage.update_checkin(ci["id"], created_at=old, last_reminder_at=old)
        noon = scheduler._now().replace(hour=12, minute=0)
        scheduler._process_ack_reminders(noon)
        assert len(sent) == 1, f"expected one reminder, got {len(sent)}"
        assert (storage.get_checkin(ci["id"]).get("reminder_count") or 0) >= 1
        # acknowledge -> no further reminders even when due
        storage.acknowledge_checkin(ci["id"])
        sent.clear()
        scheduler._process_ack_reminders(noon)
        assert len(sent) == 0, "no reminders after acknowledgment"
    finally:
        scheduler.telegram_notify.send = orig


def test_ack_reminder_silent_in_quiet_hours():
    storage.init_db()
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a), (True, "sent"))[1]
    try:
        ci = scheduler.trigger_mock_check(result="needs_darcee")
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        storage.update_checkin(ci["id"], created_at=old, last_reminder_at=old)
        sent.clear()
        midnight = scheduler._now().replace(hour=23, minute=30)
        scheduler._process_ack_reminders(midnight)
        assert len(sent) == 0, "reminders must be silent during quiet hours"
    finally:
        scheduler.telegram_notify.send = orig


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  OK  {t.__name__}")
        passed += 1
    print(f"ALL {passed} TESTS PASSED")


if __name__ == "__main__":
    _run_all()
