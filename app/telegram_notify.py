"""
Telegram notifications via the Bot API sendMessage (HTTP, stdlib only).
Never include sensitive health details. Phone numbers are masked by the caller.
"""
import json
import urllib.request

from app.config import settings

# Lightweight health surface for /guardian/health.
_status = {"last_ok": None, "last_error": None, "configured": False}


def configured():
    return bool(settings.telegram_token and settings.telegram_chat_id)


def status():
    return {
        "configured": configured(),
        "last_ok": _status["last_ok"],
        "last_error": _status["last_error"],
    }


def send(text, reply_markup=None, chat_id=None):
    """Send a Telegram message. Returns (ok, detail). No-ops cleanly if unconfigured.
    reply_markup (optional): a Telegram inline-keyboard dict (e.g. an 'I called her' button).
    chat_id (optional): override the default recipient — used to also alert family
    members (e.g. Darcee's sister) on the trash-day rider."""
    from datetime import datetime, timezone
    target = chat_id or settings.telegram_chat_id
    _status["configured"] = configured()
    if not (settings.telegram_token and target):
        _status["last_error"] = "not_configured"
        return False, "telegram_not_configured"
    try:
        payload = {
            "chat_id": target,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        _status["last_ok"] = datetime.now(timezone.utc).isoformat()
        _status["last_error"] = None
        _mirror_to_pmc(text)   # additive Echo Show feed mirror; Telegram unchanged
        return True, "sent"
    except Exception as e:
        _status["last_error"] = str(e)[:160]
        return False, str(e)[:160]


# Echo Show speaks ONLY the terminal wellness OUTCOMES — not the per-attempt
# "calling…/retrying…/starting…" play-by-play. Telegram still gets EVERYTHING
# (this runs after the Telegram send). Classify by Angel's leading status emoji:
#   💚 Mom confirmed okay                         -> normal
#   🟡 Mom wants a call (#2 / "call me")          -> important
#   🚨 escalation: all attempts, no confirmation  -> urgent (Darcee must follow up)
# Anything else (📞 calling, ⚠️ retrying, 🛡️ starting, 🧪 mock, ☎️ manual,
# ✅ ack, 🔔 incomplete call-back) is Telegram-only and never reaches the Echo.
_ECHO_PRIORITY = [("💚", "normal"), ("🟡", "important"), ("🚨", "urgent")]


def _mirror_to_pmc(text):
    """Mirror only terminal Angel OUTCOMES into PMC's Notification Router so the
    Echo Show speaks just the events Darcee cares about. Fire-and-forget: never
    blocks, never raises, never affects the Telegram send above. Progress pings
    return early (Telegram-only). destinations=['dashboard']."""
    import os
    t = (text or "").strip()
    priority = next((p for emo, p in _ECHO_PRIORITY if t.startswith(emo)), None)
    if priority is None:
        return  # progress/noise — Telegram only, no Echo Show announcement
    try:
        first = t.splitlines()[0][:120]
        payload = json.dumps({
            "token": os.environ.get("PMC_NOTIFY_TOKEN", "pmc-notify-2026"),
            "source": "Angel", "title": first, "message": t,
            "priority": priority, "destinations": ["dashboard"],
        }).encode()
        req = urllib.request.Request(
            os.environ.get("PMC_NOTIFY_URL", "http://127.0.0.1:8095/api/notifications"),
            data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass
