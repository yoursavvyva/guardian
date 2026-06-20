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


# ---- trash-day rider (Monday 12:00 PM second question) ----
def _monday_noon_iso():
    from datetime import timedelta
    base = scheduler._now().replace(hour=12, minute=0, second=0, microsecond=0)
    while base.weekday() != 0:  # advance to the next Monday
        base += timedelta(days=1)
    return base.isoformat()


def test_is_trash_check_matches_day_and_time_only():
    from datetime import datetime, timedelta
    mon = _monday_noon_iso()
    assert scheduler._is_trash_check({"scheduled_time": mon}) is True
    # right day, wrong time (8 PM check) -> no rider
    eve = datetime.fromisoformat(mon).replace(hour=20).isoformat()
    assert scheduler._is_trash_check({"scheduled_time": eve}) is False
    # right time, wrong day (Tuesday noon) -> no rider
    tue = (datetime.fromisoformat(mon) + timedelta(days=1)).isoformat()
    assert scheduler._is_trash_check({"scheduled_time": tue}) is False


def test_provider_sends_second_question_and_surfaces_answer():
    captured = {}
    _mock_voiceapp({"state": "completed", "answeredAt": "x", "confirmed": True,
                    "confirmDigit": "1", "secondDigit": "1"}, captured)
    sq = {"message": "Trash tomorrow?", "accept_digits": ["1", "2"], "yes_digit": "1",
          "reprompt": "r", "ack": {"1": "yes-ack", "2": "no-ack"}}
    res = cp.ThreeCXProvider().place_call("test", "39514", second_question=sq)
    b = captured["body"].get("secondQuestion") or {}
    assert b.get("message") == "Trash tomorrow?", b
    assert b.get("acceptDigits") == ["1", "2"], b
    assert b.get("yesDigit") == "1", b
    assert res.extra.get("second_digit") == "1", res.extra
    # baseline calls (no second_question) must NOT add the field
    captured2 = {}
    _mock_voiceapp({"state": "completed", "answeredAt": "x", "confirmed": True, "confirmDigit": "1"}, captured2)
    cp.ThreeCXProvider().place_call("test", "39514")
    assert "secondQuestion" not in captured2["body"], "baseline call must stay unchanged"


def test_scheduler_trash_yes_records_and_alerts():
    import os as _os
    storage.init_db()
    _os.environ["GUARDIAN_MOCK_SECOND_DIGIT"] = "1"
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a[0] if a else ""), (True, "sent"))[1]
    try:
        cid = storage.create_checkin(_monday_noon_iso())
        cp.set_mock_result("confirmed_ok")
        scheduler._run_attempt(storage.get_checkin(cid))
        cp.set_mock_result(None)
        ci = storage.get_checkin(cid)
        assert ci["trash_result"] == "yes", ci["trash_result"]
        assert ci["final_status"] == CheckinStatus.ANSWERED, "wellness flow must be unaffected"
        assert any("Trash Day" in str(m) for m in sent), "a 'yes' must alert"
    finally:
        scheduler.telegram_notify.send = orig
        _os.environ.pop("GUARDIAN_MOCK_SECOND_DIGIT", None)


def test_scheduler_trash_no_records_and_alerts():
    import os as _os
    storage.init_db()
    _os.environ["GUARDIAN_MOCK_SECOND_DIGIT"] = "2"
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a[0] if a else ""), (True, "sent"))[1]
    try:
        cid = storage.create_checkin(_monday_noon_iso())
        cp.set_mock_result("confirmed_ok")
        scheduler._run_attempt(storage.get_checkin(cid))
        cp.set_mock_result(None)
        ci = storage.get_checkin(cid)
        assert ci["trash_result"] == "no", ci["trash_result"]
        assert any("does NOT need" in str(m) for m in sent), "a 'no' must now also alert"
    finally:
        scheduler.telegram_notify.send = orig
        _os.environ.pop("GUARDIAN_MOCK_SECOND_DIGIT", None)


def _make_trash_checkin():
    """Run a Monday-noon mock check that records a 'yes' trash answer; return its id."""
    import os as _os
    storage.init_db()
    _os.environ["GUARDIAN_MOCK_SECOND_DIGIT"] = "1"
    try:
        cid = storage.create_checkin(_monday_noon_iso())
        cp.set_mock_result("confirmed_ok")
        scheduler._run_attempt(storage.get_checkin(cid))
        cp.set_mock_result(None)
        return cid
    finally:
        _os.environ.pop("GUARDIAN_MOCK_SECOND_DIGIT", None)


def test_trash_ack_notifies_darcee_and_is_idempotent():
    cid = _make_trash_checkin()
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a[0] if a else ""), (True, "sent"))[1]
    try:
        # the sister (a non-Darcee chat) taps "Got it"
        ci, changed = scheduler.acknowledge_trash(cid, by="Sister", by_chat="99999")
        assert changed and ci["trash_acknowledged"] == 1, ci
        assert ci["trash_acknowledged_by"] == "Sister", ci
        assert any("received Mom's trash answer" in str(m) for m in sent), "Darcee must be told"
        # a second tap must NOT re-notify or flip anything
        sent.clear()
        _ci2, changed2 = scheduler.acknowledge_trash(cid, by="Sister", by_chat="99999")
        assert changed2 is False and len(sent) == 0, "no duplicate ack/notify"
    finally:
        scheduler.telegram_notify.send = orig


def test_trash_ack_by_darcee_skips_redundant_ping():
    from app.config import settings as _s
    cid = _make_trash_checkin()
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a[0] if a else ""), (True, "sent"))[1]
    try:
        ci, changed = scheduler.acknowledge_trash(cid, by="Darcee", by_chat=str(_s.telegram_chat_id))
        assert changed and ci["trash_acknowledged"] == 1
        assert not any("received Mom's trash answer" in str(m) for m in sent), \
            "Darcee acking her own copy shouldn't ping herself"
    finally:
        scheduler.telegram_notify.send = orig


def test_non_trash_check_has_no_trash_question():
    from datetime import datetime, timedelta
    storage.init_db()
    tue = (datetime.fromisoformat(_monday_noon_iso()) + timedelta(days=1)).isoformat()
    cid = storage.create_checkin(tue)
    cp.set_mock_result("confirmed_ok")
    scheduler._run_attempt(storage.get_checkin(cid))
    cp.set_mock_result(None)
    ci = storage.get_checkin(cid)
    assert not ci.get("trash_result"), "a non-Monday check must not record a trash answer"


# ---- ANGEL-08: inbound call-back (Mom calls Angel) ----
def _silence_telegram():
    sent = []
    orig = scheduler.telegram_notify.send
    scheduler.telegram_notify.send = lambda *a, **k: (sent.append(a[0] if a else ""), (True, "sent"))[1]
    return sent, orig


def test_inbound_press1_satisfies_and_cancels_pending():
    """Press 1 on a call-back records an inbound 'answered' AND reconciles today's
    still-open scheduled check (cancels its pending retry). Approved Option 1."""
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        # A scheduled check that's mid-ladder (pending with a retry queued) — tonight's case.
        cid = storage.create_checkin(scheduler._now().isoformat())
        storage.update_checkin(cid, next_attempt_at=scheduler._now().isoformat())
        out = scheduler.handle_inbound_callback(caller="39514", digit="1")
        assert out["outcome"] == "confirmed_ok", out
        assert out["reconciled"] == 1, out
        sched = storage.get_checkin(cid)
        assert sched["final_status"] == CheckinStatus.ANSWERED, sched["final_status"]
        assert not sched["next_attempt_at"], "pending retry must be cancelled"
        inbound = storage.get_checkin(out["checkin_id"])
        assert inbound["source"] == "inbound" and inbound["wellness_result"] == "okay", inbound
        assert storage.get_meta("last_callback_outcome") == "confirmed_ok"
        assert storage.get_meta("last_callback_time")
    finally:
        scheduler.telegram_notify.send = orig


def test_inbound_press2_creates_needs_darcee_only():
    """Press 2 makes a needs_darcee call-back request (enters the ack/reminder loop) but
    does NOT satisfy a separate pending scheduled check (only press 1 satisfies)."""
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        cid = storage.create_checkin(scheduler._now().isoformat())
        out = scheduler.handle_inbound_callback(caller="39514", digit="2")
        assert out["outcome"] == "needs_darcee", out
        inbound = storage.get_checkin(out["checkin_id"])
        assert inbound["final_status"] == CheckinStatus.NEEDS_DARCEE, inbound
        assert any(p["id"] == out["checkin_id"] for p in storage.unacked_needs_darcee()), \
            "press 2 must enter the call-back reminder loop"
        # the pre-existing pending scheduled check is untouched
        assert storage.get_checkin(cid)["final_status"] == CheckinStatus.PENDING
        assert storage.get_meta("last_callback_outcome") == "needs_darcee"
    finally:
        scheduler.telegram_notify.send = orig


def test_inbound_no_press_does_not_satisfy():
    """Called but pressed nothing -> callback_no_response: notify only, satisfy nothing."""
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        cid = storage.create_checkin(scheduler._now().isoformat())
        out = scheduler.handle_inbound_callback(caller="39514", digit=None,
                                                outcome="callback_called_no_response")
        assert out["outcome"] == CheckinStatus.CALLBACK_NO_RESPONSE, out
        assert out["reconciled"] == 0, out
        inbound = storage.get_checkin(out["checkin_id"])
        assert inbound["final_status"] == CheckinStatus.CALLBACK_NO_RESPONSE, inbound
        assert not inbound["wellness_result"], "no-response must not record a wellness pass"
        assert storage.get_checkin(cid)["final_status"] == CheckinStatus.PENDING, "must NOT satisfy the check"
        assert any("didn't press" in str(m) for m in sent), "Darcee must be told she called but didn't confirm"
        assert storage.get_meta("last_callback_outcome") == CheckinStatus.CALLBACK_NO_RESPONSE
    finally:
        scheduler.telegram_notify.send = orig


def test_inbound_outcome_inferred_from_digit_when_absent():
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        assert scheduler.handle_inbound_callback(digit="1")["outcome"] == "confirmed_ok"
        assert scheduler.handle_inbound_callback(digit="2")["outcome"] == "needs_darcee"
        assert scheduler.handle_inbound_callback(digit="9")["outcome"] == CheckinStatus.CALLBACK_NO_RESPONSE
    finally:
        scheduler.telegram_notify.send = orig


def test_inbound_reconcile_skips_inbound_rows():
    """A press-1 call-back must not retro-flip earlier inbound rows (only scheduled ones)."""
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        first = storage.record_inbound_checkin(scheduler._now().isoformat(),
                                                CheckinStatus.CALLBACK_NO_RESPONSE, None)
        scheduler.handle_inbound_callback(digit="1")
        assert storage.get_checkin(first)["final_status"] == CheckinStatus.CALLBACK_NO_RESPONSE, \
            "an inbound no-response row must not be reconciled by a later press-1"
    finally:
        scheduler.telegram_notify.send = orig


# ---- ANGEL-10: Telegram control-button actions ----
def test_manual_confirm_ok_resolves_and_is_stale_safe():
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        cid = storage.create_checkin(scheduler._now().isoformat())
        storage.update_checkin(cid, next_attempt_at=scheduler._now().isoformat(),
                               final_status="escalated", escalation_sent=1)
        ci, changed = scheduler.manual_confirm_ok(cid, by="darcee", chat="123")
        assert changed, "an active/escalated check must resolve"
        assert ci["final_status"] == CheckinStatus.MANUALLY_CONFIRMED_OK, ci["final_status"]
        assert ci["source"] == "telegram_darcee" and ci["wellness_result"] == "okay", ci
        assert not ci["next_attempt_at"], "retries/escalation must be cleared"
        assert any(a["action"] == "mom_is_ok" and a["checkin_id"] == cid for a in storage.recent_audit()), \
            "the tap must be logged with who/when"
        # a SECOND (stale) tap on the now-resolved check is a no-op
        _ci2, changed2 = scheduler.manual_confirm_ok(cid, by="darcee")
        assert changed2 is False, "stale button must not re-resolve"
        # unknown id and id 0 (no active) -> no change, no crash
        ci3, changed3 = scheduler.manual_confirm_ok(999999, by="darcee")
        assert changed3 is False and ci3 is None
        assert scheduler.manual_confirm_ok(0, by="darcee")[1] is False
    finally:
        scheduler.telegram_notify.send = orig


def test_manual_confirm_does_not_touch_newer_answered_check():
    """A stale 'Mom is OK' button (older id) must never flip a different/newer check-in."""
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        old = storage.create_checkin(scheduler._now().isoformat())   # still pending
        new = scheduler.trigger_mock_check(result="confirmed_ok")["id"]  # already answered
        # tapping the OLD button only ever targets `old`; `new` is untouched
        scheduler.manual_confirm_ok(old, by="darcee")
        assert storage.get_checkin(new)["final_status"] == CheckinStatus.ANSWERED, "newer check unchanged"
        assert storage.get_checkin(old)["final_status"] == CheckinStatus.MANUALLY_CONFIRMED_OK
    finally:
        scheduler.telegram_notify.send = orig


def test_trigger_check_now_is_telegram_darcee_and_audited():
    storage.init_db()
    sent, orig = _silence_telegram()
    cp.set_mock_result("confirmed_ok")
    try:
        ci = scheduler.trigger_check_now(by="test")
        assert ci["source"] == "telegram_darcee", ci["source"]
        assert ci["final_status"] == CheckinStatus.ANSWERED
        assert any(a["action"] == "call_now" for a in storage.recent_audit())
    finally:
        cp.set_mock_result(None)
        scheduler.telegram_notify.send = orig


def test_pause_blocks_scheduled_then_resume_restores():
    import os as _os
    storage.init_db()
    sent, orig = _silence_telegram()
    cp.set_mock_result("confirmed_ok")
    try:
        now = scheduler._now()
        _os.environ["GUARDIAN_SCHEDULE"] = now.strftime("%H:%M")
        slot = now.replace(second=0, microsecond=0).isoformat()
        scheduler.pause_today(by="test")
        assert scheduler.is_paused_today() is True
        scheduler._tick()
        assert storage.checkin_exists_for(slot) is None, "paused: scheduled check must NOT fire"
        scheduler.resume_checks(by="test")
        assert scheduler.is_paused_today() is False
        scheduler._tick()
        assert storage.checkin_exists_for(slot) is not None, "resumed: scheduled check fires"
        assert any(a["action"] == "pause_today" for a in storage.recent_audit())
        assert any(a["action"] == "resume" for a in storage.recent_audit())
    finally:
        cp.set_mock_result(None)
        _os.environ.pop("GUARDIAN_SCHEDULE", None)
        storage.set_meta("paused_date", "")
        scheduler.telegram_notify.send = orig


def test_status_line_reflects_pause():
    storage.init_db()
    sent, orig = _silence_telegram()
    try:
        assert "Angel / Guardian status" in scheduler.status_line()
        scheduler.pause_today(by="test")
        assert "PAUSED" in scheduler.status_line()
        scheduler.resume_checks(by="test")
        assert "PAUSED" not in scheduler.status_line()
    finally:
        storage.set_meta("paused_date", "")
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
