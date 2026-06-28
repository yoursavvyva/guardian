"""
Guardian scheduler — stdlib background thread (no external deps).

Each day it fires the configured wellness checks (default 11:00, 16:00, 20:30
America/New_York).

Option B — Alexa is the preferred at-home channel: when a check-in window opens,
Guardian WAITS GUARDIAN_ALEXA_GRACE_MINUTES (default 15) for Mom to confirm via
Alexa ("Alexa, tell Guardian Angel I'm okay"). An Alexa confirmation during (or
just before) the window satisfies the check and CANCELS the phone call. Only if no
Alexa confirmation arrives in the grace period does Angel place the fallback call.
Set GUARDIAN_ALEXA_GRACE_MINUTES=0 for the legacy call-first behavior.

When Angel does call, it runs an attempt ladder with retries:

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
    # Prefer the configured real pickup day (e.g. "Tuesday"); else the next-day fallback.
    if settings.trash_pickup_day:
        return settings.trash_pickup_day
    try:
        return (datetime.fromisoformat(checkin["scheduled_time"]).astimezone(_tz())
                + timedelta(days=1)).strftime("%A")
    except (ValueError, TypeError):
        return "tomorrow"


def _pickup_after(sched):
    """The pickup datetime for a trash question asked at `sched`: the next occurrence of the
    configured pickup day on/after the ask date (e.g. asked Sunday → Tuesday), or next-day."""
    if settings.trash_pickup_day:
        try:
            target = _DAYS.index(settings.trash_pickup_day.strip().lower())
        except ValueError:
            return sched + timedelta(days=1)
        return sched + timedelta(days=(target - sched.weekday()) % 7)
    return sched + timedelta(days=1)


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


# ---- ANGEL-14: STANDALONE trash sequence (its own call AFTER wellness completes) ----
def _today_trash_checkin(now=None):
    """Today's standalone trash check-in (source='trash'), or None."""
    now = now or _now()
    start, end = _today_bounds(now)
    for ci in storage.checkins_between(start.isoformat(), end.isoformat(), limit=50):
        if ci.get("source") == "trash":
            return ci
    return None


def _trash_anchor_dt(now):
    """Today at trash_time (the noon wellness slot the trash sequence follows)."""
    hh, mm = (int(x) for x in settings.trash_time.strip().split(":"))
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _wellness_call_pending(now):
    """True if ANY open wellness check still has a call queued (next_attempt_at). Used to
    guarantee the trash call NEVER competes with a wellness call."""
    for ci in storage.open_checkins():
        if ci.get("source") in (None, "scheduled") and ci.get("next_attempt_at"):
            return True
    return False


def _wellness_complete_for_trash(now):
    """True only once today's noon WELLNESS sequence is fully finished: the noon check
    exists, is no longer pending, has no queued call, and no other wellness call is queued.
    This is the gate that keeps the trash sequence from ever interfering with wellness."""
    anchor = _trash_anchor_dt(now)
    if anchor > now:
        return False
    nid = storage.checkin_exists_for(anchor.isoformat())
    if not nid:
        return False
    noon = storage.get_checkin(nid)
    if not noon or noon.get("final_status") == CheckinStatus.PENDING or noon.get("next_attempt_at"):
        return False
    return not _wellness_call_pending(now)


def _trash_voice_groups():
    yes_ph = settings.voice_trash_yes_phrases
    no_ph = settings.voice_trash_no_phrases
    if yes_ph and no_ph:
        return [{"digit": settings.trash_yes_digit, "phrases": yes_ph},
                {"digit": settings.trash_no_digit, "phrases": no_ph}]
    return None


def _trash_standalone_primary():
    """The single yes/no question for the standalone trash call (replaces the wellness menu)."""
    return {
        "message": settings.trash_standalone_message,
        "reprompt": settings.trash_standalone_reprompt,
        "accept_digits": [settings.trash_yes_digit, settings.trash_no_digit],
        "confirm_digit": settings.trash_yes_digit,
        "ack": {settings.trash_yes_digit: settings.trash_ack_yes,
                settings.trash_no_digit: settings.trash_ack_no},
        "voice_groups": _trash_voice_groups(),
    }


def _trash_darcee_buttons(checkin_id):
    """Yes/No buttons for Darcee to record Mom's trash answer after following up herself."""
    return {"inline_keyboard": [[
        {"text": "✅ Yes — goes out", "callback_data": f"guardian_trash_set:{checkin_id}:yes"},
        {"text": "❌ No", "callback_data": f"guardian_trash_set:{checkin_id}:no"}]]}


def _create_trash_checkin(now):
    """Open today's standalone trash check: Alexa grace window first, then one call."""
    cid = storage.create_checkin(now.isoformat())
    grace = settings.alexa_grace_minutes
    first_call = now + timedelta(minutes=grace)
    storage.update_checkin(cid, source="trash", next_attempt_at=first_call.isoformat())
    storage.add_audit("trash_seq_open", "guardian", None, cid, f"grace={grace}m")
    pickup_name = settings.trash_pickup_day or (now + timedelta(days=1)).strftime("%A")
    if grace:
        telegram_notify.send(
            f"🗑️ Angel: opening the trash check for {pickup_name}'s pickup. Mom can tell Alexa her "
            f"answer — Angel will call by {_label(first_call.isoformat())} if she hasn't.")
    return cid


def _run_trash_call(tci):
    """Place the ONE standalone trash call (no wellness menu). Records yes/no, or starts the
    callback window if she didn't give an answer."""
    provider = get_provider()
    if settings.mom_extension:
        ttype, tval = TargetType.EXTENSION, settings.mom_extension
    else:
        ttype, tval = TargetType.CELL, settings.mom_cell
    masked = mask_phone(tval)
    attempt_id = storage.create_attempt(tci["id"], tci["scheduled_time"], 1, ttype, masked, provider.name)
    telegram_notify.send(f"📞 Angel: calling Mom with the trash question ({ttype} {masked}).")
    res = provider.place_call(ttype, tval, primary=_trash_standalone_primary())
    storage.finish_attempt(attempt_id, res.status, res.error)
    storage.update_checkin(tci["id"], attempt_count=1, next_attempt_at=None)

    digit = str((res.extra or {}).get("primary_digit") or "")
    answer = ("yes" if digit == settings.trash_yes_digit
              else ("no" if digit == settings.trash_no_digit else None))
    if answer:
        storage.update_checkin(tci["id"], trash_result=answer, final_status=CheckinStatus.ANSWERED)
        storage.add_audit("trash_call_answered", "mom (call)", None, tci["id"], answer)
        _notify_trash(storage.get_checkin(tci["id"]), answer)
    else:
        # No yes/no — give Mom the callback window before asking Darcee to follow up.
        storage.update_checkin(tci["id"], last_reminder_at=_now().isoformat())
        storage.add_audit("trash_call_unanswered", "guardian", None, tci["id"], res.status)
        telegram_notify.send(
            f"🗑️ Angel called Mom about the trash but didn't get a yes or no ({res.status}). "
            f"Giving her {settings.trash_callback_window_minutes} min to call back before I ask you to follow up.")


def _alert_darcee_trash(tci):
    """Callback window elapsed with no answer → ask Darcee to follow up + set the answer."""
    storage.update_checkin(tci["id"], escalation_sent=1)
    storage.add_audit("trash_ask_darcee", "guardian", None, tci["id"], None)
    tomorrow = _trash_tomorrow(tci)
    telegram_notify.send(
        "🗑️ Trash — needs your follow-up\n\n"
        f"I couldn't get a yes or no from Mom about {tomorrow}'s pickup (one call + a "
        f"{settings.trash_callback_window_minutes}-minute window). Please check with her, then tap "
        "her answer below — I'll let your sister know and she'll confirm she got it.",
        reply_markup=_trash_darcee_buttons(tci["id"]))


def set_trash_answer(checkin_id, answer, by="darcee", chat=None):
    """Darcee taps Yes/No to record Mom's trash answer after following up. Reuses the same
    sister-ack notification as a Mom-given answer. Idempotent; won't override a real answer."""
    ci = storage.get_checkin(checkin_id) if checkin_id else None
    if not ci or ci.get("source") != "trash":
        return None, False
    if ci.get("trash_result"):
        return ci, False  # already answered (e.g. Mom called back) — don't override
    if answer not in ("yes", "no"):
        return ci, False
    storage.update_checkin(checkin_id, trash_result=answer, final_status=CheckinStatus.ANSWERED)
    storage.add_audit("trash_set_darcee", by, chat, checkin_id, answer)
    _notify_trash(storage.get_checkin(checkin_id), answer)
    return storage.get_checkin(checkin_id), True


def handle_alexa_trash(answer):
    """Mom answers the trash question via Alexa during the grace window (or anytime it's open).
    Records the answer, cancels the pending call, and fires the sister-ack notification."""
    now = _now()
    tci = _today_trash_checkin(now)
    if not tci or tci.get("trash_result") or tci.get("final_status") != CheckinStatus.PENDING:
        return {"ok": False, "error": "no open trash question"}
    if answer not in ("yes", "no"):
        return {"ok": False, "error": "unknown answer"}
    storage.update_checkin(tci["id"], trash_result=answer, final_status=CheckinStatus.ANSWERED,
                           next_attempt_at=None)
    storage.set_meta("last_callback_time", now.isoformat())
    storage.set_meta("last_callback_outcome", f"alexa_trash_{answer}")
    storage.add_audit("alexa_trash", "mom (alexa)", None, tci["id"], answer)
    _notify_trash(storage.get_checkin(tci["id"]), answer)
    return {"ok": True, "answer": answer, "checkin_id": tci["id"]}


def _tick_trash(now):
    """Drive the standalone trash sequence. Runs AFTER wellness in each tick and only ever
    places its call once wellness is fully done — wellness always wins."""
    if not (settings.trash_enabled and settings.trash_standalone):
        return
    if not _active(now) or is_paused_today(now):
        return
    if now.strftime("%A").lower() != settings.trash_day.strip().lower():
        return
    tci = _today_trash_checkin(now)
    if tci is None:
        if _wellness_complete_for_trash(now):
            _create_trash_checkin(now)
        return
    if tci.get("trash_result") or tci.get("final_status") != CheckinStatus.PENDING:
        return  # already answered/closed
    # Phase 1: waiting for Alexa, then place the ONE call when the grace window elapses.
    if (tci.get("attempt_count") or 0) == 0:
        nxt = tci.get("next_attempt_at")
        try:
            due = bool(nxt) and datetime.fromisoformat(nxt) <= now
        except ValueError:
            due = False
        if due and not _wellness_call_pending(now):
            _run_trash_call(tci)
        return
    # Phase 2: call placed, no answer → wait the callback window, then alert Darcee once.
    if tci.get("escalation_sent"):
        return
    base = tci.get("last_reminder_at")
    try:
        ready = (now - datetime.fromisoformat(base)) >= timedelta(minutes=settings.trash_callback_window_minutes)
    except (ValueError, TypeError):
        ready = bool(base)
    if ready:
        _alert_darcee_trash(tci)


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _next_trash_ask(now=None):
    """Next datetime the trash question will ride along (configured day + time), >= now."""
    now = now or _now()
    try:
        target_dow = _DAYS.index(settings.trash_day.strip().lower())
        hh, mm = (int(x) for x in settings.trash_time.strip().split(":"))
    except (ValueError, IndexError):
        return None
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (target_dow - cand.weekday()) % 7
    cand = cand + timedelta(days=delta)
    if cand <= now:
        cand = cand + timedelta(days=7)
    return cand


def trash_status(now=None):
    """Trash-day rollup for the dashboard: the latest answer, which pickup it's for,
    whether it's still current, and when Mom will next be asked."""
    if not settings.trash_enabled:
        return {"enabled": False}
    now = now or _now()
    ci = storage.latest_trash_answer()
    out = {
        "enabled": True,
        "answer": None,            # "yes" | "no" | raw digit | None
        "collected_at": None,      # ISO time of the check-in that collected it
        "pickup_day": None,        # e.g. "Tuesday"
        "pickup_date": None,       # ISO date of that pickup
        "acknowledged": False,
        "acknowledged_by": None,
        "current": False,          # True while that pickup is still today/upcoming
        "next_ask_at": None,       # when the trash question next rides along
    }
    nxt = _next_trash_ask(now)
    out["next_ask_at"] = nxt.isoformat() if nxt else None
    if ci and ci.get("trash_result"):
        try:
            sched = datetime.fromisoformat(ci["scheduled_time"]).astimezone(_tz())
            pickup = _pickup_after(sched)
        except (ValueError, TypeError):
            pickup = None
        out["answer"] = ci["trash_result"]
        out["collected_at"] = ci["scheduled_time"]
        out["acknowledged"] = bool(ci.get("trash_acknowledged"))
        out["acknowledged_by"] = ci.get("trash_acknowledged_by")
        if pickup:
            out["pickup_day"] = pickup.strftime("%A")
            out["pickup_date"] = pickup.date().isoformat()
            # The answer is "current" until the end of the pickup day it refers to.
            out["current"] = pickup.date() >= now.date()
    return out


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
    # ANGEL-14: when the STANDALONE trash sequence is enabled, the wellness call never carries
    # the rider — trash gets its own separate call after wellness finishes.
    second_q = (_trash_question()
                if (not settings.trash_standalone and _is_trash_check(checkin)
                    and not checkin.get("trash_result"))
                else None)

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
        if ci.get("source") not in (None, "scheduled"):
            continue  # only reconcile real SCHEDULED checks (not inbound/alexa/telegram rows)
        if ci["final_status"] in open_states:
            storage.update_checkin(ci["id"], final_status=CheckinStatus.ANSWERED,
                                   wellness_result="okay", next_attempt_at=None)
            count += 1
    return count


# ---- Option B: Alexa-preferred at-home channel (grace period before Angel calls) ----
def _alexa_handled_window(since_dt):
    """True if Mom already used the Alexa channel (said okay OR asked for Darcee) at or
    after `since_dt`. Used at window-open so a proactive Alexa check-in just BEFORE the
    slot stops Guardian from opening a phone window at all. Scoped to the alexa_* meta
    outcomes so a phone call-back never trips it."""
    if storage.get_meta("last_callback_outcome") not in ("alexa_confirmed_ok", "alexa_needs_darcee"):
        return False
    raw = storage.get_meta("last_callback_time")
    try:
        return datetime.fromisoformat(raw) >= since_dt
    except (TypeError, ValueError):
        return False


def _cancel_open_scheduled_calls(now=None):
    """An Alexa 'I need Darcee' during the grace window means Mom has engaged from home —
    close today's still-open SCHEDULED phone windows and cancel their (deferred) calls so
    Angel doesn't ring her for a wellness check right after. Closes them as ANSWERED with
    wellness_result='needs_call' (NOT a second needs_darcee row — the inbound Alexa row owns
    the call-back request + ack loop). Returns how many were cancelled."""
    now = now or _now()
    start, end = _today_bounds(now)
    open_states = (CheckinStatus.PENDING, CheckinStatus.MISSED, CheckinStatus.ESCALATED)
    count = 0
    for ci in storage.checkins_between(start.isoformat(), end.isoformat(), limit=50):
        if ci.get("source") not in (None, "scheduled"):
            continue
        if ci["final_status"] in open_states:
            storage.update_checkin(ci["id"], final_status=CheckinStatus.ANSWERED,
                                   wellness_result="needs_call", next_attempt_at=None)
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

    # Call-back-specific outcome LABELS (distinct from the canonical final_status used
    # internally). These are what we report/store + return to the voice-app.
    label = {"confirmed_ok": "callback_confirmed_ok",
             "needs_darcee": "callback_needs_darcee",
             CheckinStatus.CALLBACK_NO_RESPONSE: "callback_no_response"}[outcome]

    # Always record for reporting, regardless of outcome.
    storage.set_meta("last_callback_time", now.isoformat())
    storage.set_meta("last_callback_outcome", label)
    if masked:
        storage.set_meta("last_callback_caller", masked)

    if outcome == "confirmed_ok":
        cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.ANSWERED, "okay")
        reconciled = _reconcile_today_satisfied(now)
        tail = (" Today's pending check is now satisfied and I've stopped any pending retries."
                if reconciled else "")
        telegram_notify.send(
            "💚 Angel Call-Back\n\nMom called Angel back and confirmed she's okay (pressed 1)." + tail + " 💛")
        # Follow-ups (e.g. the recovered Monday trash question) are built AFTER reconcile
        # so the missed Monday-noon check is detectable. They never affect the wellness result.
        return {"outcome": label, "checkin_id": cid, "reconciled": reconciled,
                "followups": _callback_followups(now)}

    if outcome == "needs_darcee":
        cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.NEEDS_DARCEE, "needs_call")
        telegram_notify.send(
            "🟡 Angel Call-Back\n\n"
            "Mom called Angel and asked for a call from Darcee (pressed 2).\n\n"
            "This is not an emergency, but she would like you to call her.\n"
            "Tap below once you've called her.",
            reply_markup=_ack_button(cid))
        return {"outcome": label, "checkin_id": cid, "reconciled": 0,
                "followups": _callback_followups(now)}

    # callback_no_response: she called but pressed nothing — does NOT satisfy the wellness check.
    cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.CALLBACK_NO_RESPONSE, None)
    telegram_notify.send(
        "🔔 Angel Call-Back\n\n"
        "Mom called Angel but didn't press 1 or 2, so the check-in isn't complete.\n"
        "She may just be checking in, but you may want to reach out to her to be sure.")
    # Still offer the trash follow-up (asked regardless of the wellness answer).
    return {"outcome": label, "checkin_id": cid, "reconciled": 0,
            "followups": _callback_followups(now)}


# ---- ANGEL-09: Alexa wellness channel (reuses the ANGEL-08 reconcile) ----
def handle_alexa_wellness(intent):
    """Alexa relays Mom's spoken wellness response from home ("Alexa, tell Angel I'm okay"
    / "…I need Darcee"). Reuses the ANGEL-08 reconcile so 'okay' SATISFIES + cancels today's
    pending phone check, and 'needs_darcee' opens a call-back request. Records source='alexa';
    Telegram wording clearly says the confirmation came via ALEXA. Returns a result dict."""
    now = _now()
    intent = (intent or "").strip().lower()
    if intent in ("okay", "ok", "im_okay", "confirmed_ok", "1"):
        cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.ANSWERED, "okay", source="alexa")
        reconciled = _reconcile_today_satisfied(now)
        storage.set_meta("last_callback_time", now.isoformat())
        storage.set_meta("last_callback_outcome", "alexa_confirmed_ok")
        storage.add_audit("alexa_okay", "mom (alexa)", None, cid, f"reconciled={reconciled}")
        tail = (" Today's pending check is satisfied and any pending phone retries are cancelled."
                if reconciled else "")
        telegram_notify.send("💚 Alexa Check-In\n\nMom told Alexa she's okay (at home)." + tail + " 💛")
        return {"ok": True, "outcome": "alexa_confirmed_ok", "checkin_id": cid,
                "reconciled": reconciled, "source": "alexa"}
    if intent in ("needs_darcee", "need_darcee", "darcee", "2"):
        cid = storage.record_inbound_checkin(now.isoformat(), CheckinStatus.NEEDS_DARCEE, "needs_call", source="alexa")
        cancelled = _cancel_open_scheduled_calls(now)  # don't phone-check her right after she used Alexa
        storage.set_meta("last_callback_time", now.isoformat())
        storage.set_meta("last_callback_outcome", "alexa_needs_darcee")
        storage.add_audit("alexa_needs_darcee", "mom (alexa)", None, cid, f"cancelled={cancelled}")
        telegram_notify.send(
            "🟡 Alexa Check-In\n\n"
            "Mom asked Alexa to have Darcee call her (at home).\n\n"
            "This is not an emergency, but she would like you to call her.\n"
            "Tap below once you've called her.",
            reply_markup=_ack_button(cid))
        return {"ok": True, "outcome": "alexa_needs_darcee", "checkin_id": cid,
                "reconciled": cancelled, "source": "alexa"}
    return {"ok": False, "error": "unknown intent", "source": "alexa"}


# ---- ANGEL-08 add-on: extensible call-back follow-up questions (trash = the first) ----
def _pending_trash_callback(now=None):
    """The Monday-12:00-PM trash-rider check TODAY that hasn't captured a trash answer yet
    (i.e. Mom missed it). Returns that check-in row, or None. This is the ONLY gate for the
    trash follow-up on call-backs: `_is_trash_check` requires day==trash_day AND
    time==trash_time, so a non-Monday call-back or the 8 PM check never qualifies."""
    now = now or _now()
    if not settings.trash_enabled:
        return None
    # ANGEL-14: standalone trash check open today (Mom calling back can still answer it).
    if settings.trash_standalone:
        tci = _today_trash_checkin(now)
        if tci and not tci.get("trash_result") and tci.get("final_status") == CheckinStatus.PENDING:
            return tci
        return None
    start, end = _today_bounds(now)
    for ci in storage.checkins_between(start.isoformat(), end.isoformat(), limit=50):
        if ci.get("source") == "inbound":
            continue
        if _is_trash_check(ci) and not ci.get("trash_result"):
            return ci
    return None


def _callback_followups(now=None):
    """Build the list of follow-up questions to ask after the wellness menu on a call-back.
    EXTENSIBLE: each entry is a self-contained question the voice-app asks then reports via
    /guardian/inbound/followup. Today only the recovered Monday trash question; add more keys
    here for future task/reminder questions."""
    now = now or _now()
    out = []
    tci = _pending_trash_callback(now)
    if tci:
        tq = _trash_question()
        out.append({
            "key": "trash",
            "target_checkin_id": tci["id"],
            "message": tq["message"],
            "reprompt": tq["reprompt"],
            "accept_digits": tq["accept_digits"],
            "yes_digit": tq["yes_digit"],
            "ack": tq["ack"],
        })
    return out


def record_callback_followup(checkin_id, key, digit=None):
    """Record a call-back follow-up answer SEPARATELY from the wellness outcome (a trash
    answer must never change the wellness status). Dispatches by `key`; idempotent.
    Returns a small result dict. Extend with new keys for future questions."""
    digit = str(digit) if digit not in (None, "") else None
    if key == "trash":
        ci = storage.get_checkin(int(checkin_id)) if checkin_id else None
        # Accept the legacy rider check OR the ANGEL-14 standalone trash check (source='trash').
        if not ci or not (_is_trash_check(ci) or ci.get("source") == "trash"):
            return {"ok": False, "key": key, "error": "not a trash check"}
        if ci.get("trash_result"):
            return {"ok": True, "key": key, "trash_outcome": ci["trash_result"], "already": True}
        if digit == settings.trash_yes_digit:
            answer, tout = "yes", "trash_needed"
        elif digit == settings.trash_no_digit:
            answer, tout = "no", "trash_not_needed"
        else:
            answer, tout = "unknown", "trash_unknown"
        storage.update_checkin(ci["id"], trash_result=answer)
        # The standalone trash row IS the trash item, so its own status closes out here.
        if ci.get("source") == "trash" and answer in ("yes", "no"):
            storage.update_checkin(ci["id"], final_status=CheckinStatus.ANSWERED, next_attempt_at=None)
        storage.add_audit("callback_trash", "mom (callback)", None, ci["id"], tout)
        # yes/no reuse the SAME notification the original Monday call would have sent
        # (🗑️ alert + sister + "Got it" button). 'unknown' is logged only — never blocks wellness.
        if answer in ("yes", "no"):
            _notify_trash(ci, answer)
        return {"ok": True, "key": key, "trash_outcome": tout}
    return {"ok": False, "key": key, "error": "unknown followup key"}


# ---- ANGEL-10: Telegram control-button actions (explicit, auditable; NOT open commands) ----
def manual_confirm_ok(checkin_id, by="darcee", chat=None):
    """Darcee taps 'Mom is OK' to manually resolve an ACTIVE check-in: status
    manually_confirmed_ok, source telegram_darcee, retries/escalation cleared.
    Staleness guard: only resolves a still-open (pending/escalated/missed) check, so a
    stale button can never flip an already-resolved or newer check-in. Returns (ci, changed)."""
    ci = storage.get_checkin(checkin_id) if checkin_id else None
    if not ci:
        return None, False
    open_states = (CheckinStatus.PENDING, CheckinStatus.ESCALATED, CheckinStatus.MISSED)
    if ci["final_status"] not in open_states:
        return ci, False  # already resolved/terminal — ignore the (stale) tap
    ci = storage.resolve_manual_ok(checkin_id, by=by)
    storage.add_audit("mom_is_ok", by, chat, checkin_id, ci["scheduled_time"] if ci else None)
    label = _label(ci["scheduled_time"]) if ci else ""
    telegram_notify.send(
        f"✅ You marked Mom OK for the {label} check. Retries and escalation are cleared. 💛")
    return ci, True


def trigger_check_now(by="darcee", chat=None):
    """Place an on-demand Angel wellness call to Mom right now (real provider when live).
    Creates a check-in tagged source=telegram_darcee and runs the first attempt; the normal
    retry/escalation ladder then applies. The confirmation step lives in the Telegram UI."""
    now = _now()
    cid = storage.create_checkin(now.isoformat())
    storage.update_checkin(cid, source="telegram_darcee")
    storage.add_audit("call_now", by, chat, cid, "on-demand wellness call")
    telegram_notify.send(f"☎️ Angel: placing a wellness call to Mom now (requested by {by}).")
    _run_attempt(storage.get_checkin(cid))
    return storage.get_checkin(cid)


def is_paused_today(now=None):
    """True if Darcee paused today's remaining scheduled checks (auto-clears tomorrow)."""
    return storage.get_meta("paused_date") == (now or _now()).date().isoformat()


def pause_today(by="darcee", chat=None):
    now = _now()
    storage.set_meta("paused_date", now.date().isoformat())
    storage.add_audit("pause_today", by, chat, None, now.date().isoformat())
    telegram_notify.send(
        f"⏸ Angel: today's remaining wellness checks are PAUSED (by {by}). "
        "In-progress checks finish; new ones won't start. Tap Resume to re-enable — auto-resumes tomorrow.")
    return True


def resume_checks(by="darcee", chat=None):
    was_paused = is_paused_today()
    storage.set_meta("paused_date", "")
    storage.add_audit("resume", by, chat, None, "")
    telegram_notify.send(
        f"▶️ Angel: wellness checks RESUMED (by {by}). Next check: {next_scheduled_check() or 'n/a'}.")
    return was_paused


def status_line():
    """Compact, read-only status summary for the Telegram 'Status' button."""
    now = _now()
    st = state()
    paused = is_paused_today(now)
    start, end = _today_bounds(now)
    today = storage.checkins_between(start.isoformat(), end.isoformat(), limit=50)
    done = sum(1 for c in today if c["final_status"] in (CheckinStatus.ANSWERED, CheckinStatus.MANUALLY_CONFIRMED_OK))
    pend = sum(1 for c in today if c["final_status"] == CheckinStatus.PENDING)
    esc = sum(1 for c in today if c["final_status"] == CheckinStatus.ESCALATED)
    nd = sum(1 for c in today if c["final_status"] == CheckinStatus.NEEDS_DARCEE)
    lines = [
        "🛡️ Angel / Guardian status",
        f"Provider: {settings.call_provider} · Scheduler: {'running' if st['running'] else 'stopped'}"
        + (" · ⏸ PAUSED today" if paused else ""),
        f"Schedule: {', '.join(settings.schedule)} ({settings.timezone})",
        f"Next check: {('paused — resumes tomorrow' if paused else (st['next_scheduled_check'] or 'n/a'))}",
        f"Today: {done} done · {pend} pending · {nd} needs-Darcee · {esc} escalated",
    ]
    pcb = storage.unacked_needs_darcee()
    if pcb:
        lines.append(f"⚠️ {len(pcb)} call-back(s) awaiting your 'I called Mom'.")
    lc = storage.get_meta("last_callback_outcome")
    if lc:
        lines.append(f"Last call-back: {lc} @ {_label(storage.get_meta('last_callback_time') or '')}")
    return "\n".join(lines)


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
    # 1) fire due scheduled checks (within the recent window only) — not before go-live date,
    #    and not while Darcee has paused today's remaining checks (ANGEL-10).
    fire = _active(now) and not is_paused_today(now)
    grace = settings.alexa_grace_minutes
    for sched in (_scheduled_today(now) if fire else []):
        if sched <= now and (now - sched) <= timedelta(minutes=FIRE_WINDOW_MIN):
            if not storage.checkin_exists_for(sched.isoformat()):
                # Option B: if Mom already used Alexa at/just-before this window, don't open
                # a phone window at all — the inbound Alexa check-in is the record.
                if grace and _alexa_handled_window(sched - timedelta(minutes=grace)):
                    continue
                cid = storage.create_checkin(sched.isoformat())
                if grace:
                    # Open the window and WAIT for Alexa; defer Angel's first call by the
                    # grace period (reuses the retry path below). An Alexa "okay" during the
                    # grace window reconciles this row + clears next_attempt_at -> no call.
                    first_call = sched + timedelta(minutes=grace)
                    storage.update_checkin(cid, next_attempt_at=first_call.isoformat())
                    telegram_notify.send(
                        f"🛡️ Guardian: the {_label(sched.isoformat())} check-in window is open. "
                        f"Waiting for Mom's Alexa check-in — Angel will call by {_label(first_call.isoformat())} "
                        f"if she hasn't confirmed.")
                else:
                    telegram_notify.send(f"🛡️ Guardian: starting the {_label(sched.isoformat())} wellness check.")
                    _run_attempt(storage.get_checkin(cid))
    # 2) process due retries on open check-ins (and the Option-B deferred first call)
    for ci in storage.open_checkins():
        if ci.get("source") == "trash":
            continue  # ANGEL-14: standalone trash rows are driven by _tick_trash, not the wellness ladder
        nxt = ci.get("next_attempt_at")
        if not nxt:
            continue
        try:
            due = datetime.fromisoformat(nxt) <= now
        except ValueError:
            due = False
        if not due:
            continue
        # The deferred FIRST call (no attempts yet) respects the same go-live/pause gate as
        # step 1, and won't fire for a stale window (e.g. a check that sat paused all day).
        if (ci.get("attempt_count") or 0) == 0:
            try:
                sched_dt = datetime.fromisoformat(ci["scheduled_time"])
                stale = (now - sched_dt) > timedelta(minutes=grace + FIRE_WINDOW_MIN + 1)
            except (ValueError, TypeError, KeyError):
                stale = False
            if not fire or stale:
                if stale:
                    storage.update_checkin(ci["id"], next_attempt_at=None)
                continue
        _run_attempt(ci)
    # 3) ANGEL-14: drive the standalone trash sequence (only ever after wellness is done)
    _tick_trash(now)
    # 4) re-nudge un-acknowledged call-back requests (ANGEL-06)
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
