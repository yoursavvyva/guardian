"""
Guardian scheduler — stdlib background thread (no external deps).

Each day it fires the configured wellness checks (default 11:00, 16:00, 20:30
America/New_York). Per check it runs an attempt ladder with retries:

  attempt 1 -> Mom's 3CX extension
  wait GUARDIAN_RETRY_MINUTES (default 20)
  attempt 2 -> extension again
  wait GUARDIAN_RETRY_MINUTES
  attempt 3 -> Mom's cell (backup)
  all fail  -> urgent Telegram escalation

A tick loop (every 30s) creates due check-ins and processes due retries, so the
20-minute waits don't block anything. State is persisted in SQLite, so a restart
resumes in-progress check-ins.
"""
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import storage, telegram_notify
from app.call_provider import get_provider, set_mock_result
from app.config import settings, mask_phone
from app.models import AttemptStatus, CheckinStatus, TargetType

TICK_SECONDS = 30
FIRE_WINDOW_MIN = 15   # don't retroactively fire checks missed while the service was down

_state = {"started_at": None, "last_run_at": None, "running": False}
_thread = None


def _tz():
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return ZoneInfo("America/New_York")


def _now():
    return datetime.now(_tz())


def _scheduled_today(now=None):
    """Today's scheduled datetimes (tz-aware) from GUARDIAN_SCHEDULE."""
    now = now or _now()
    out = []
    for hm in settings.schedule:
        try:
            h, m = [int(x) for x in hm.split(":")]
            out.append(now.replace(hour=h, minute=m, second=0, microsecond=0))
        except (ValueError, IndexError):
            continue
    return out


def next_scheduled_check(now=None):
    now = now or _now()
    candidates = _scheduled_today(now) + [d + timedelta(days=1) for d in _scheduled_today(now)]
    future = sorted(d for d in candidates if d > now)
    return future[0].isoformat() if future else None


# ---- targets ----
def _target_for(attempt_number):
    last = attempt_number >= settings.max_attempts
    if last and settings.mom_cell:
        return TargetType.CELL, settings.mom_cell
    if settings.mom_extension:
        return TargetType.EXTENSION, settings.mom_extension
    if settings.mom_cell:
        return TargetType.CELL, settings.mom_cell
    return TargetType.EXTENSION, "unconfigured"


# ---- attempt execution ----
def _run_attempt(checkin):
    attempt_number = (checkin.get("attempt_count") or 0) + 1
    ttype, tval = _target_for(attempt_number)
    masked = mask_phone(tval)
    provider = get_provider()

    attempt_id = storage.create_attempt(
        checkin["id"], checkin["scheduled_time"], attempt_number, ttype, masked, provider.name)
    telegram_notify.send(f"📞 Guardian: calling Mom — attempt {attempt_number} ({ttype} {masked}).")

    res = provider.place_call(ttype, tval)
    storage.finish_attempt(attempt_id, res.status, res.error)
    storage.update_checkin(checkin["id"], attempt_count=attempt_number)

    label = _label(checkin["scheduled_time"])
    # ANGEL-05: pressing 1 (okay) is the only wellness pass.
    if res.status == "confirmed_ok":
        storage.update_checkin(checkin["id"], final_status=CheckinStatus.ANSWERED,
                               wellness_result="okay",
                               answered_attempt_number=attempt_number, next_attempt_at=None)
        telegram_notify.send(f"💚 Guardian: Mom confirmed she's okay (pressed 1) on the {label} check. All good.")
        return

    # ANGEL-05: pressing 2 means "have Darcee call me" — a COMPLETED check (not a failure).
    # Terminal, no retry/escalation; ping Darcee right away.
    if res.status == "needs_darcee":
        storage.update_checkin(checkin["id"], final_status=CheckinStatus.NEEDS_DARCEE,
                               wellness_result="needs_call",
                               answered_attempt_number=attempt_number, next_attempt_at=None)
        telegram_notify.send(
            "🟡 Angel Check-In\n\n"
            "Mom requested a call from Darcee.\n\n"
            f"Time: {label}\n\n"
            "This is not an emergency, but she would like you to call her.")
        return

    # Everything else advances the ladder. Distinguish the cause for clear wording:
    #   answered_unconfirmed → connected but no key press (voicemail/auto-answer/no input)
    #   failed               → technical/system problem (NOT "Mom missed")
    #   missed               → genuine no-answer
    technical = res.status == "failed"
    unconfirmed = res.status == "answered_unconfirmed"
    if attempt_number >= settings.max_attempts:
        storage.update_checkin(checkin["id"], final_status=CheckinStatus.ESCALATED,
                               escalation_sent=1, next_attempt_at=None)
        if technical:
            telegram_notify.send(
                f"🚨 Guardian couldn't COMPLETE the wellness calls (technical issue: {res.error}) "
                f"after {settings.max_attempts} attempts (check {label}). Likely a phone/system problem, "
                f"not necessarily Mom — please check on her directly and check Guardian.")
        elif unconfirmed:
            telegram_notify.send(
                f"🚨 Guardian reached the line for the {label} check but Mom never CONFIRMED she's okay "
                f"(no key press) after {settings.max_attempts} attempts. Please check on her directly.")
        else:
            telegram_notify.send(
                f"🚨 Guardian could not reach Mom after {settings.max_attempts} attempts "
                f"(check {label}). Check Alexa/camera or call her directly.")
    else:
        retry_at = (_now() + timedelta(minutes=settings.retry_minutes))
        storage.update_checkin(checkin["id"], next_attempt_at=retry_at.isoformat())
        when = "now" if settings.retry_minutes <= 0 else f"in {settings.retry_minutes} min"
        if technical:
            telegram_notify.send(
                f"⚠️ Guardian: couldn't complete the call (technical: {res.error}, attempt {attempt_number}). "
                f"Retrying {when}.")
        elif unconfirmed:
            telegram_notify.send(
                f"⚠️ Guardian: call connected but Mom didn't press 1 to confirm (attempt {attempt_number}). "
                f"Retrying {when}.")
        else:
            telegram_notify.send(
                f"⚠️ Guardian: Mom didn't answer (attempt {attempt_number}). Retrying {when}.")


def _label(scheduled_iso):
    try:
        return datetime.fromisoformat(scheduled_iso).astimezone(_tz()).strftime("%-I:%M %p")
    except Exception:
        return scheduled_iso


# ---- tick ----
def _tick():
    now = _now()
    # 1) fire due scheduled checks (within the recent window only)
    for sched in _scheduled_today(now):
        if sched <= now and (now - sched) <= timedelta(minutes=FIRE_WINDOW_MIN):
            if not storage.checkin_exists_for(sched.isoformat()):
                cid = storage.create_checkin(sched.isoformat())
                telegram_notify.send(f"🛡️ Guardian: starting the {_label(sched.isoformat())} wellness check.")
                _run_attempt(storage.get_checkin(cid))
    # 2) process due retries on open check-ins
    for ci in storage.open_checkins():
        nxt = ci.get("next_attempt_at")
        if nxt:
            try:
                due = datetime.fromisoformat(nxt) <= now
            except ValueError:
                due = False
            if due:
                _run_attempt(ci)


def trigger_mock_check(result=None):
    """Test-only: run an immediate wellness check now (mock provider)."""
    if result:
        set_mock_result(result)
    now = _now()
    cid = storage.create_checkin(now.isoformat())
    telegram_notify.send("🧪 Guardian: running a MOCK wellness check (test).")
    _run_attempt(storage.get_checkin(cid))
    if result:
        set_mock_result(None)
    return storage.get_checkin(cid)


def _loop():
    _state["running"] = True
    while True:
        try:
            _tick()
            _state["last_run_at"] = datetime.now(_tz()).isoformat()
        except Exception as e:  # never let the scheduler die
            print(f"[guardian.scheduler] tick error: {e}", flush=True)
        time.sleep(TICK_SECONDS)


def start():
    global _thread
    storage.init_db()
    _state["started_at"] = _now().isoformat()
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="guardian-scheduler", daemon=True)
    _thread.start()


def state():
    return {
        "running": _state["running"] and bool(_thread and _thread.is_alive()),
        "started_at": _state["started_at"],
        "last_run_at": _state["last_run_at"],
        "next_scheduled_check": next_scheduled_check(),
    }
