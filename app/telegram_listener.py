"""
Guardian's own Telegram listener — runs on the ANGEL bot (momsguardianangel_bot),
completely separate from Max/PMC. Long-polls for inline-button taps (callback_query)
and a SMALL fixed set of commands that ONLY summon button panels.

ANGEL-10 design rule: **Angel is not Max.** It exposes only explicit, auditable
safety BUTTONS — never open-ended command execution. The only text it reacts to is
`/menu`/`/status` (and aliases), which just render the control panel or a read-only
status; all real actions happen via inline buttons (with a confirm step before any
call). Every mutating tap is authorized to Darcee and logged (storage.add_audit).

Why Guardian owns this: the Angel bot is a different bot/token from the PMC bot that
max-bot polls, so there is NO poller conflict and NO dependency on Max.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

from app.config import settings

_thread = None

_MENU_COMMANDS = {"/menu", "/start", "/angel", "/panel", "/controls"}


def _api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.telegram_token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=50) as r:
        return json.load(r)


def _answer(cb_id, text, alert=False):
    if not cb_id:
        return
    try:
        kw = {"callback_query_id": cb_id, "text": text}
        if alert:
            kw["show_alert"] = "true"
        _api("answerCallbackQuery", **kw)
    except Exception:
        pass


def _send(chat, text, keyboard=None):
    try:
        params = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if keyboard:
            params["reply_markup"] = json.dumps(keyboard)
        _api("sendMessage", **params)
    except Exception:
        pass


def _edit(chat, message_id, text, keyboard=None):
    try:
        _api("editMessageText", chat_id=chat, message_id=message_id, text=text,
             reply_markup=json.dumps(keyboard if keyboard is not None else {"inline_keyboard": []}))
    except Exception:
        pass


def _is_darcee(chat):
    """Control buttons + the menu are Darcee-only (her configured chat)."""
    return bool(settings.telegram_chat_id) and str(chat) == str(settings.telegram_chat_id)


# ---- control panel ----
def _panel_keyboard():
    """Build the control panel. Embeds the CURRENT active check-in id into 'Mom is OK'
    (0 if none) so a later-rendered panel can't resolve an older check — and vice-versa,
    a stale panel only ever points at its own (by-then resolved) id."""
    from app import scheduler, storage
    now = scheduler._now()
    start, end = scheduler._today_bounds(now)
    today = storage.checkins_between(start.isoformat(), end.isoformat(), limit=50)
    active = next((c for c in today
                   if c["final_status"] in ("pending", "escalated", "missed")), None)
    active_id = active["id"] if active else 0
    pcb = storage.unacked_needs_darcee()
    nd_id = pcb[0]["id"] if pcb else 0
    paused = scheduler.is_paused_today(now)
    rows = [
        [{"text": "✅ Mom is OK", "callback_data": f"guardian_ok:{active_id}"}],
        [{"text": "📞 I called Mom", "callback_data": f"guardian_ack:{nd_id}"}],
        [{"text": "☎️ Call Mom now", "callback_data": "guardian_callnow"}],
    ]
    rows.append([{"text": "▶️ Resume checks", "callback_data": "guardian_resume"}] if paused
                else [{"text": "⏸ Pause checks today", "callback_data": "guardian_pause"}])
    rows.append([{"text": "📊 Status", "callback_data": "guardian_status"}])
    return {"inline_keyboard": rows}


def _send_panel(chat):
    _send(chat, "🛡️ Angel controls — tap an action:", _panel_keyboard())


# ---- message handling: ONLY summon panels; never execute actions from text ----
def _handle_message(msg):
    chat = str((msg.get("chat") or {}).get("id") or "")
    text = (msg.get("text") or "").strip().lower()
    if not _is_darcee(chat):
        return  # Angel ignores everyone else, and ALL non-command text (it is not a command bot)
    if text in _MENU_COMMANDS:
        _send_panel(chat)
    elif text == "/status":
        from app import scheduler
        _send(chat, scheduler.status_line())
    # anything else: silently ignored — no open-ended commands, no text-triggered calls


# ---- callback (button) handling ----
def _handle_trash_ack(scheduler, data, cb_id, msg, chat, who):
    try:
        cid = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        cid = None
    ci, changed = (None, False)
    if cid is not None:
        ci, changed = scheduler.acknowledge_trash(cid, by=str(who), by_chat=chat)
    acked = bool(ci and ci.get("trash_acknowledged"))
    _answer(cb_id, "✅ Thanks — got it. Darcee has been notified." if changed
            else ("Already acknowledged. 👍" if acked else "Couldn't find that one."), alert=True)
    if msg.get("message_id"):
        _edit(chat, msg["message_id"], (msg.get("text") or "Trash Day") + f"\n\n✅ Acknowledged by {who}.")


def _handle_trash_set(scheduler, data, cb_id, msg, chat, who):
    """Darcee taps Yes/No to record Mom's trash answer (ANGEL-14 follow-up). callback_data:
    guardian_trash_set:<checkin_id>:<yes|no>."""
    parts = data.split(":")
    cid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    answer = parts[2] if len(parts) > 2 else None
    ci, changed = (None, False)
    if cid and answer in ("yes", "no"):
        ci, changed = scheduler.set_trash_answer(cid, answer, by=f"{who} (telegram)", chat=chat)
    if changed:
        verb = "goes out" if answer == "yes" else "does NOT go out"
        _answer(cb_id, f"✅ Recorded: trash {verb}. Your sister has been notified to confirm.", alert=True)
        if msg.get("message_id"):
            _edit(chat, msg["message_id"], (msg.get("text") or "Trash") + f"\n\n✅ You answered: {answer.upper()}.")
    else:
        already = bool(ci and ci.get("trash_result"))
        _answer(cb_id, f"Already answered ({ci.get('trash_result','').upper()}). 👍" if already
                else "Couldn't find that trash question.", alert=True)


def _handle_called_mom(scheduler, data, cb_id, msg, chat):
    try:
        cid = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        cid = None
    ci, _changed = (None, False)
    if cid:
        ci, _changed = scheduler.acknowledge_and_confirm(cid, by="darcee (telegram)")
    acked = bool(ci and ci.get("acknowledged"))
    _answer(cb_id, "✅ Thank you — I've noted you called Mom. I'll stop reminding you." if acked
            else ("Nothing is waiting for a call-back right now. 👍" if not cid else "Already marked as called. 👍"),
            alert=True)
    if cid and msg.get("message_id"):
        _edit(chat, msg["message_id"], (msg.get("text") or "Angel call-back") + "\n\n✅ Marked as called.")


def _handle_mom_is_ok(scheduler, data, cb_id, msg, chat, who):
    try:
        cid = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        cid = 0
    ci, changed = scheduler.manual_confirm_ok(cid, by=f"{who} (telegram)", chat=chat)
    if changed:
        _answer(cb_id, "✅ Marked Mom OK. Retries and escalation cleared.", alert=True)
        if msg.get("message_id"):
            _edit(chat, msg["message_id"], (msg.get("text") or "Angel") + f"\n\n✅ Mom marked OK by {who}.")
    else:
        # Stale/already-resolved tap — never flips a newer or already-closed check.
        _answer(cb_id, "That check-in is no longer active (already resolved)." if ci
                else "No active check-in to resolve right now.", alert=True)


def _handle_callnow(scheduler, data, cb_id, msg, chat):
    if data == "guardian_callnow":
        # Step 1: require explicit confirmation before any real dial.
        _answer(cb_id, "Confirm below to place the call.")
        if msg.get("message_id"):
            _edit(chat, msg["message_id"],
                  "☎️ Call Mom now?\n\nThis places a real Angel wellness call to Mom right now.",
                  {"inline_keyboard": [[
                      {"text": "✅ Yes, call now", "callback_data": "guardian_callnow_yes"},
                      {"text": "✖️ Cancel", "callback_data": "guardian_callnow_no"}]]})
    elif data == "guardian_callnow_yes":
        _answer(cb_id, "☎️ Calling Mom now…", alert=True)
        if msg.get("message_id"):
            _edit(chat, msg["message_id"], "☎️ Placing a wellness call to Mom now…")
        try:
            scheduler.trigger_check_now(by="darcee (telegram)", chat=chat)
        except Exception as e:
            _send(chat, f"⚠️ Couldn't place the call: {str(e)[:140]}")
    elif data == "guardian_callnow_no":
        _answer(cb_id, "Cancelled.")
        if msg.get("message_id"):
            _edit(chat, msg["message_id"], "☎️ Call cancelled.")


def _handle_pause_resume(scheduler, data, cb_id, msg, chat, who):
    if data == "guardian_pause":
        scheduler.pause_today(by=f"{who} (telegram)", chat=chat)
        _answer(cb_id, "⏸ Paused today's remaining checks.", alert=True)
    else:
        scheduler.resume_checks(by=f"{who} (telegram)", chat=chat)
        _answer(cb_id, "▶️ Checks resumed.", alert=True)
    if msg.get("message_id"):
        _edit(chat, msg["message_id"], "🛡️ Angel controls — tap an action:", _panel_keyboard())


def _handle_callback(cb):
    from app import scheduler  # late import to avoid a cycle
    data = cb.get("data") or ""
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id") or "")
    frm = cb.get("from") or {}
    who = frm.get("first_name") or frm.get("username") or "Someone"

    is_trash = data.startswith("guardian_trash_ack:")
    # Authorize: trash acks also allow the sister (GUARDIAN_TRASH_CHAT_IDS); every other
    # control action is Darcee-only.
    if is_trash:
        allowed = {str(settings.telegram_chat_id)} if settings.telegram_chat_id else set()
        allowed |= {str(c) for c in settings.trash_extra_chat_ids}
        if allowed and chat and chat not in allowed:
            return _answer(cb_id, "Not authorized.")
        return _handle_trash_ack(scheduler, data, cb_id, msg, chat, who)

    if not _is_darcee(chat):
        return _answer(cb_id, "Not authorized.")

    if data.startswith("guardian_trash_set:"):
        return _handle_trash_set(scheduler, data, cb_id, msg, chat, who)
    if data.startswith("guardian_ok:"):
        return _handle_mom_is_ok(scheduler, data, cb_id, msg, chat, who)
    if data.startswith("guardian_ack:"):
        return _handle_called_mom(scheduler, data, cb_id, msg, chat)
    if data.startswith("guardian_callnow"):
        return _handle_callnow(scheduler, data, cb_id, msg, chat)
    if data in ("guardian_pause", "guardian_resume"):
        return _handle_pause_resume(scheduler, data, cb_id, msg, chat, who)
    if data == "guardian_status":
        _answer(cb_id, "📊 Sending status…")
        return _send(chat, scheduler.status_line(), _panel_keyboard())
    if data == "guardian_panel":
        _answer(cb_id, "")
        return _send_panel(chat)
    _answer(cb_id, "Unknown action.")


def _loop():
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["callback_query", "message"])}
            if offset is not None:
                params["offset"] = offset
            r = _api("getUpdates", **params)
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if upd.get("callback_query"):
                    _handle_callback(upd["callback_query"])
                elif upd.get("message"):
                    _handle_message(upd["message"])
        except Exception as e:  # never let the listener die
            print(f"[guardian.tg-listener] {str(e)[:120]}", flush=True)
            time.sleep(3)


def start():
    """Start the Angel-bot listener (no-op if no token configured)."""
    global _thread
    if not settings.telegram_token:
        print("[guardian.tg-listener] no telegram token — listener disabled", flush=True)
        return
    if _thread and _thread.is_alive():
        return
    # Register the bot's command hints (purely cosmetic; both only render button panels).
    try:
        _api("setMyCommands", commands=json.dumps([
            {"command": "menu", "description": "Angel control buttons"},
            {"command": "status", "description": "Angel / Guardian status"},
        ]))
    except Exception:
        pass
    _thread = threading.Thread(target=_loop, name="guardian-tg-listener", daemon=True)
    _thread.start()
    print("[guardian.tg-listener] listening on the Angel bot (buttons + /menu, /status)", flush=True)
