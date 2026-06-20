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


def _active(now=None):
    """False before the configured go-live date (so no real calls fire early)."""
    sd = settings.start_date
    return sd is None or (now or _now()).date() >= sd


def next_scheduled_check(now=None):
    now = now or _now()
    sd = settings.start_date
    candidates = _scheduled_today(now) + [d + timedelta(days=1) for d in _scheduled_today(now)]
    future = sorted(d for d in candidates if d > now and (sd is None or d.date() >= sd))
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


# ---- trash-day rider ----
def _is_trash_check(checkin):
    """True if this check-in is the configured day+time that carries the trash question."""
    if not settings.trash_enabled:
        return False
    try:
        dt = datetime.fromisoformat(checkin["scheduled_time"]).astimezone(_tz())
    except (ValueError, TypeError):
        return False
    return (dt.strftime("%A").lower() == settings.trash_day.strip().lower()
            and dt.strftime("%H:%M") == settings.trash_time.strip())


def _trash_question():
    return {
        "message": settings.trash_message,
        "accept_digits": [settings.trash_yes_digit, settings.trash_no_digit],
        "yes_digit": settings.trash_yes_digit,
        "reprompt": settings.trash_reprompt,
        "ack": {settings.trash_yes_digit: settings.trash_ack_yes,
                settings.trash_no_digit: settings.trash_ack_no},
    }


def _trash_tomorrow(checkin):
    try:
        return (datetime.fromisoformat(checkin["scheduled_time"]).astimezone(_tz())
                + timedelta(days=1)).strftime("%A")
    except (ValueError, TypeError):
        return "tomorrow"


def _trash_ack_button(checkin_id):
    """Inline 'Got it' button on the trash alert; the sister (or Darcee) taps to confirm receipt."""
    return {"inline_keyboard": [[{"text": "✅ Got it", "callback_data": f"guardian_trash_ack:{checkin_id}"}]]}


def _notify_trash(checkin, answer):
    """Alert Darcee + extra family chat IDs (sister) of Mom's trash answer — YES or NO —
    each with a 'Got it' button so the sister acknowledges she received it."""
    label = _label(checkin["scheduled_time"])
    tomorrow = _trash_tomorrow(checkin)
    if answer == "yes":
        line = f"Mom says the trash NEEDS to go out for {tomorrow}. 🗑️"
    elif answer == "no":
        line = f"Mom says the trash does NOT need to go out for {tomorrow}."
    else:
        line = f"Mom's trash answer for {tomorrow}: {answer}."
    msg = ("🗑️ Trash Day\n\n" + line + f"\n(from the {label} check-in)\n\n"
           "Tap below to confirm you got this.")
    btn = _trash_ack_button(checkin["id"])
    telegram_notify.send(msg, reply_markup=btn)
    for cid in settings.trash_extra_chat_ids:
        telegram_notify.send(msg, reply_markup=btn, chat_id=cid)


def acknowledge_trash(checkin_id, by="sister", by_chat=None):
    """Mark a trash answer as received and tell Darcee her sister got it.
    Returns (checkin, changed). Idempotent — only the first ack notifies Darcee."""
    ci = storage.get_checkin(checkin_id)
    if not ci or not ci.get("trash_result"):
        return ci, False
    if ci.get("trash_acknowledged"):
        return ci, False  # already acknowledged — don't double-notify
    ci = storage.acknowledge_trash_checkin(checkin_id, by=by)
    # Tell Darcee it was received (skip the redundant ping if Darcee acked it herself).
    if str(by_chat or "") != str(settings.telegram_chat_id):
        ans = (ci or {}).get("trash_result")
        verb = "needs to go out" if ans == "yes" else ("does NOT need to go out" if ans == "no" else str(ans))
        telegram_notify.send(
            f"✅ {by} confirmed they received Mom's trash answer — it {verb} "
            f"for {_trash_tomorrow(ci)}. You're all set. 💛")
    return ci, True


# ---- attempt execution ----
def _run_attempt(checkin):
    attempt_number = (checkin.get("attempt_count") or 0) + 1
    ttype, tval = _target_for(attempt_number)
    masked = mask_phone(tval)
    provider = get_provider()

    attempt_id = storage.create_attempt(
        checkin["id"], checkin["scheduled_time"], attempt_number, ttype, masked, provider.name)
    telegram_notify.send(f"📞 Guardian: calling Mom — attempt {attempt_number} ({ttype} {masked}).")

    # Trash-day rider: only ask if it's the configured check AND she hasn't answered it yet.
    second_q = _trash_question() if (_is_trash_check(checkin) and not checkin.get("trash_result")) else None

    res = provider.place_call(ttype, tval, second_question=second_q)
    storage.finish_attempt(attempt_id, res.status, res.error)
    storage.update_checkin(checkin["id"], attempt_count=attempt_number)

    # Record + notify the trash answer once, before the wellness outcome's early returns.
    # Notify on BOTH yes and no, each with a "Got it" ack button for the sister.
    if second_q and (res.extra or {}).get("second_digit") and not checkin.get("trash_result"):
        sd = str(res.extra["second_digit"])
        answer = "yes" if sd == settings.trash_yes_digit else ("no" if sd == settings.trash_no_digit else sd)
        storage.update_checkin(checkin["id"], trash_result=answer)
        _notify_trash(checkin, answer)

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
            "This is not an emergency, but she would like you to call her.\n"
            "Tap below once you've called her.",
            reply_markup=_ack_button(checkin["id"]))
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


# ---- ANGEL-06: call-back acknowledgment reminders ----
def _ack_button(checkin_id):
    """Inline 'I called her' button; tapping it sends callback_data Guardian's bot poller acts on."""
    return {"inline_keyboard": [[{"text": "✅ I called her", "callback_data": f"guardian_ack:{checkin_id}"}]]}


def acknowledge_and_confirm(checkin_id, by="darcee"):
    """Mark a needs_darcee call-back acknowledged and send Angel's confirmation back.
    Used by BOTH the API (dashboard button) and the Angel-bot listener (Telegram tap),
    so there is exactly one place that acks + confirms. Returns (checkin, changed)."""
    ci = storage.get_checkin(checkin_id)
    if not ci or ci.get("final_status") != CheckinStatus.NEEDS_DARCEE:
        return ci, False
    if ci.get("acknowledged"):
        return ci, False  # already acknowledged — don't send a duplicate confirmation
    ci = storage.acknowledge_checkin(checkin_id, by=by)
    label = _label(ci["scheduled_time"]) if ci else ""
    telegram_notify.send(
        "✅ Thank you, Darcee. I've noted that you called Mom back"
        + (f" about her {label} request" if label else "")
        + ". I'll stop reminding you now. 💛")
    return ci, True


# ---- ANGEL-08: inbound call-back (Mom calls Angel) ----
def _today_bounds(now=None):
    now = now or _now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _reconcile_today_satisfied(now=None):
    """An explicit press-1 call-back satisfies today's still-open SCHEDULED checks and
    cancels any pending retry/escalation (approved design, Option 1). Inbound rows are
    skipped. Returns how many scheduled check-ins were reconciled."""
    now = now or _now()
    start, end = _today_bounds(now)
    open_states = (CheckinStatus.PENDING, CheckinStatus.MISSED, CheckinStatus.ESCALATED)
    count = 0
    for ci in storage.checkins_between(start.isoformat(), end.isoformat(), limit=50):
        if ci.get("source") == "inbound":
            continue
        if ci["final_status"] in open_states:
            storage.update_checkin(ci["id"], final_status=CheckinStatus.ANSWERED,
                                   wellness_result="okay", next_attempt_at=None)
            count += 1
    return count


def handle_inbound_callback(caller=None, digit=None, outcome=None):
    """Mom called Angel back. Records the call-back + last_callback_time/outcome (reporting),
    then routes by outcome:
      press 1 (confirmed_ok)        -> satisfy today's pending check, cancel retries, 💚 ping
      press 2 (needs_darcee)        -> open a call-back request (ack/reminder loop), 🟡 ping
      no key  (callback_no_response)-> NOTHING is satisfied; 🔔 ping so Darcee can reach out
    Only an explicit press-1 satisfies the wellness check; only press-2 makes a needs_darcee
    request (per Darcee, 2026-06-19). Returns a small result dict."""
    now = _now()
    masked = mask_phone(caller) if caller else None
    digit = str(digit) if digit not in (None, "") else None

    # Normalize the outcome (prefer the explicit one from the voice-app; else infer from the digit).
    if outcome == "callback_called_no_response":
        outcome = CheckinStatus.CALLBACK_NO_RESPONSE
    valid = ("confirmed_ok", "needs_darcee", CheckinStatus.CALLBACK_NO_RESPONSE)
    if outcome not in valid:
        if digit == settings.okay_digit:
            outcome = "confirmed_ok"
        elif digit == settings.needs_call_digit:
            outcome = "needs_darcee"
        else:
            outcome = CheckinStatus.CALLBACK_NO_RESPONSE

    # Always record for reporting, regardless of outcome.
    storage.set_meta("last_callback_time", now.isoformat())
    storage.set_meta("last_callback_outcome", outcome)
    if masked:
        storage.set_meta("last_callback_caller", masked)

    if outcome == "confirmed_ok":
        cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.ANSWERED, "okay")
        reconciled = _reconcile_today_satisfied(now)
        tail = (" Today's pending check is now satisfied and I've stopped any pending retries."
                if reconciled else "")
        telegram_notify.send(
            "💚 Angel Call-Back\n\nMom called Angel back and confirmed she's okay (pressed 1)." + tail + " 💛")
        return {"outcome": outcome, "checkin_id": cid, "reconciled": reconciled}

    if outcome == "needs_darcee":
        cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.NEEDS_DARCEE, "needs_call")
        telegram_notify.send(
            "🟡 Angel Call-Back\n\n"
            "Mom called Angel and asked for a call from Darcee (pressed 2).\n\n"
            "This is not an emergency, but she would like you to call her.\n"
            "Tap below once you've called her.",
            reply_markup=_ack_button(cid))
        return {"outcome": outcome, "checkin_id": cid, "reconciled": 0}

    # callback_no_response: she called but pressed nothing — does NOT satisfy the wellness check.
    cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.CALLBACK_NO_RESPONSE, None)
    telegram_notify.send(
        "🔔 Angel Call-Back\n\n"
        "Mom called Angel but didn't press 1 or 2, so the check-in isn't complete.\n"
        "She may just be checking in, but you may want to reach out to her to be sure.")
    return {"outcome": outcome, "checkin_id": cid, "reconciled": 0}


def _in_quiet_hours(now=None):
    """True if local time is within the no-reminder window (quiet_start..quiet_end, wrapping midnight)."""
    h = (now or _now()).hour
    s, e = settings.quiet_start_hour, settings.quiet_end_hour
    if s == e:
        return False
    return (h >= s or h < e) if s > e else (s <= h < e)


def _process_ack_reminders(now=None):
    """Re-nudge Darcee every ack_reminder_minutes for any un-acknowledged needs_darcee
    check-in, until she taps acknowledge. Quiet overnight; resumes in the morning."""
    now = now or _now()
    if _in_quiet_hours(now):
        return
    from datetime import timezone
    interval = timedelta(minutes=settings.ack_reminder_minutes)
    now_utc = datetime.now(timezone.utc)
    for ci in storage.unacked_needs_darcee():
        base = ci.get("last_reminder_at") or ci.get("created_at")
        try:
            since = now_utc - datetime.fromisoformat(base)
        except (ValueError, TypeError):
            since = interval  # malformed timestamp → treat as due
        if since < interval:
            continue
        label = _label(ci["scheduled_time"])
        telegram_notify.send(
            "🔔 Reminder — Mom still waiting for a call\n\n"
            f"She asked Angel for a call from you at {label} and it isn't marked done yet.\n"
            "Tap below once you've called her (or clear it on the Guardian page).",
            reply_markup=_ack_button(ci["id"]))
        storage.mark_reminded(ci["id"])


# ---- tick ----
def _tick():
    now = _now()
    # 1) fire due scheduled checks (within the recent window only) — not before go-live date
    for sched in (_scheduled_today(now) if _active(now) else []):
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
    # 3) re-nudge un-acknowledged call-back requests (ANGEL-06)
    _process_ack_reminders(now)


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
