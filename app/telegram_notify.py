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


def _mirror_to_pmc(text):
    """Mirror an Angel notification into PMC's Notification Router (Echo Show
    Integration · Phase 01). Fire-and-forget: never blocks, never raises, never
    affects the Telegram send above. destinations=['dashboard'] only — no second
    Telegram message, just a stored copy for the future Echo Show 8 dashboard.
    Title = first line of the message for a tidier dashboard card."""
    import os
    try:
        first = (text or "").strip().splitlines()[0][:120] if text else ""
        payload = json.dumps({
            "token": os.environ.get("PMC_NOTIFY_TOKEN", "pmc-notify-2026"),
            "source": "Angel", "title": first, "message": text or "",
            "priority": "normal", "destinations": ["dashboard"],
        }).encode()
        req = urllib.request.Request(
            os.environ.get("PMC_NOTIFY_URL", "http://127.0.0.1:8095/api/notifications"),
            data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass
