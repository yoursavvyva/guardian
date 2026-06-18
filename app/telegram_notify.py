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


def send(text, reply_markup=None):
    """Send a Telegram message. Returns (ok, detail). No-ops cleanly if unconfigured.
    reply_markup (optional): a Telegram inline-keyboard dict (e.g. an 'I called her' button)."""
    from datetime import datetime, timezone
    _status["configured"] = configured()
    if not configured():
        _status["last_error"] = "not_configured"
        return False, "telegram_not_configured"
    try:
        payload = {
            "chat_id": settings.telegram_chat_id,
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
        return True, "sent"
    except Exception as e:
        _status["last_error"] = str(e)[:160]
        return False, str(e)[:160]
