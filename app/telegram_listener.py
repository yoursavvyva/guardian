"""
Guardian's own Telegram listener — runs on the ANGEL bot (momsguardianangel_bot),
completely separate from Max/PMC. It only long-polls for inline-button taps
(callback_query) so Darcee can acknowledge "I called her" right from the alert.

Why Guardian owns this: the Angel bot is a different bot/token from the PMC bot
that max-bot polls, so there is NO poller conflict and NO dependency on Max.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

from app.config import settings

_thread = None


def _api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{settings.telegram_token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=50) as r:
        return json.load(r)


def _handle_callback(cb):
    from app import scheduler  # late import to avoid a cycle
    data = cb.get("data") or ""
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id") or "")
    frm = cb.get("from") or {}
    who = frm.get("first_name") or frm.get("username") or "Someone"
    is_trash = data.startswith("guardian_trash_ack:")
    # Authorize: needs_darcee acks are Darcee-only; trash acks also allow the sister
    # (any chat in GUARDIAN_TRASH_CHAT_IDS), since she's the one confirming receipt.
    allowed = {str(settings.telegram_chat_id)} if settings.telegram_chat_id else set()
    if is_trash:
        allowed |= {str(c) for c in settings.trash_extra_chat_ids}
    if allowed and chat and chat not in allowed:
        if cb_id:
            try:
                _api("answerCallbackQuery", callback_query_id=cb_id, text="Not authorized.")
            except Exception:
                pass
        return
    if is_trash:
        try:
            cid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            cid = None
        ci, changed = (None, False)
        if cid is not None:
            ci, changed = scheduler.acknowledge_trash(cid, by=str(who), by_chat=chat)
        acked = bool(ci and ci.get("trash_acknowledged"))
        if cb_id:
            try:
                _api("answerCallbackQuery", callback_query_id=cb_id, show_alert="true",
                     text="✅ Thanks — got it. Darcee has been notified."
                          if changed else ("Already acknowledged. 👍" if acked else "Couldn't find that one."))
            except Exception:
                pass
        if msg.get("message_id"):
            try:
                _api("editMessageText", chat_id=chat, message_id=msg["message_id"],
                     text=(msg.get("text") or "Trash Day") + f"\n\n✅ Acknowledged by {who}.",
                     reply_markup=json.dumps({"inline_keyboard": []}))
            except Exception:
                pass
        return
    if data.startswith("guardian_ack:"):
        try:
            cid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            cid = None
        ci, _changed = (None, False)
        if cid is not None:
            ci, _changed = scheduler.acknowledge_and_confirm(cid, by="darcee (telegram)")
        acked = bool(ci and ci.get("acknowledged"))
        if cb_id:
            try:
                # show_alert => a clear popup she must dismiss, not an easy-to-miss toast.
                _api("answerCallbackQuery", callback_query_id=cb_id, show_alert="true",
                     text="✅ Thank you — I've noted you called Mom. I'll stop reminding you."
                          if acked else "Already marked as called. 👍")
            except Exception:
                pass
        # Always strip the button + annotate the message so it visibly resolves (whether
        # this tap or an earlier one did the ack) — empty inline_keyboard removes it.
        if msg.get("message_id"):
            try:
                _api("editMessageText", chat_id=chat, message_id=msg["message_id"],
                     text=(msg.get("text") or "Angel call-back") + "\n\n✅ Marked as called.",
                     reply_markup=json.dumps({"inline_keyboard": []}))
            except Exception:
                pass


def _loop():
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["callback_query"])}
            if offset is not None:
                params["offset"] = offset
            r = _api("getUpdates", **params)
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if upd.get("callback_query"):
                    _handle_callback(upd["callback_query"])
        except Exception as e:  # never let the listener die
            print(f"[guardian.tg-listener] {str(e)[:120]}", flush=True)
            time.sleep(3)


def start():
    """Start the Angel-bot callback listener (no-op if no token configured)."""
    global _thread
    if not settings.telegram_token:
        print("[guardian.tg-listener] no telegram token — listener disabled", flush=True)
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="guardian-tg-listener", daemon=True)
    _thread.start()
    print("[guardian.tg-listener] listening on the Angel bot for ack taps", flush=True)
