"""
Guardian HTTP API (stdlib http.server) — internal/admin-only, bound to 127.0.0.1.

PMC consumes these endpoints (it never touches the SQLite file). Optional bearer
token via GUARDIAN_API_TOKEN (sent as X-Guardian-Token). Phone numbers/secrets are
never returned — only masked forms.
"""
import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app import storage, telegram_notify, scheduler
from app.config import settings, mask_phone
from app.models import CheckinStatus

START_TS = datetime.now()


def _today_bounds():
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat(), now


def health():
    sch = scheduler.state()
    try:
        storage.last_checkin()
        db_ok = "ok"
    except Exception as e:
        db_ok = f"error: {str(e)[:80]}"
    return {
        "service_status": "online",
        "scheduler_status": "running" if sch["running"] else "stopped",
        "last_run_at": sch["last_run_at"],
        "next_scheduled_check": sch["next_scheduled_check"],
        "telegram_status": telegram_notify.status(),
        "call_provider": settings.call_provider,
        "database_status": db_ok,
        "started_at": sch["started_at"],
    }


def status():
    s, e, now = _today_bounds()
    today = storage.checkins_between(s, e, limit=50)
    last = storage.last_checkin()
    answered = storage.last_answered()
    missed_today = sum(1 for c in today if c["final_status"] in (CheckinStatus.MISSED, CheckinStatus.ESCALATED))
    escalation_active = any(c["final_status"] == CheckinStatus.ESCALATED for c in today)
    # overall today rollup for the dashboard dot
    if escalation_active:
        today_status = "red"
    elif any(c["final_status"] == CheckinStatus.PENDING for c in today):
        today_status = "yellow"
    elif today and all(c["final_status"] == CheckinStatus.ANSWERED for c in today):
        today_status = "green"
    elif today:
        today_status = "yellow"
    else:
        today_status = "idle"
    return {
        "today_status": today_status,
        "last_check_status": last["final_status"] if last else None,
        "last_answered_at": answered["scheduled_time"] if answered else None,
        "missed_checks_today": missed_today,
        "escalation_active": escalation_active,
        "next_check_at": scheduler.next_scheduled_check(),
    }


def checkins_today():
    s, e, now = _today_bounds()
    today = storage.checkins_between(s, e, limit=50)
    sched = scheduler._scheduled_today(now)
    completed = [c for c in today if c["final_status"] == CheckinStatus.ANSWERED]
    missed = [c for c in today if c["final_status"] in (CheckinStatus.MISSED, CheckinStatus.ESCALATED)]
    pending = [c for c in today if c["final_status"] == CheckinStatus.PENDING]
    return {
        "scheduled": [d.strftime("%-I:%M %p") for d in sched],
        "completed": len(completed),
        "missed": len(missed),
        "pending": len(pending),
        "escalation": any(c["final_status"] == CheckinStatus.ESCALATED for c in today),
        "checkins": today,
    }


def config_overview():
    """Masked, non-sensitive configuration snapshot for the report page."""
    return {
        "timezone": settings.timezone,
        "schedule": settings.schedule,
        "retry_minutes": settings.retry_minutes,
        "max_attempts": settings.max_attempts,
        "call_provider": settings.call_provider,
        "mock_result": settings.mock_result if settings.call_provider == "mock" else None,
        "mom_extension": mask_phone(settings.mom_extension) or "(not set)",
        "mom_cell": mask_phone(settings.mom_cell) or "(not set)",
        "telegram_configured": telegram_notify.configured(),
        "telnyx_configured": bool(settings.telnyx_api_key),
        "threecx_configured": bool(settings.threecx_extension and settings.threecx_auth_id),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet; PM2 captures stdout
        pass

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authed(self):
        token = settings.api_token
        if not token:
            return True  # no token configured → rely on localhost binding
        return self.headers.get("X-Guardian-Token") == token

    def _qs(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/guardian/health", "/health"):
            return self._send(200, health())
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if path == "/guardian/status":
            return self._send(200, status())
        if path == "/guardian/checkins/today":
            return self._send(200, checkins_today())
        if path == "/guardian/checkins":
            q = self._qs()
            return self._send(200, {"checkins": storage.checkins_between(
                q.get("date_from"), q.get("date_to"), int(q.get("limit", 50)))})
        if path == "/guardian/attempts":
            q = self._qs()
            return self._send(200, {"attempts": storage.attempts_between(
                q.get("checkin_id"), q.get("date_from"), q.get("date_to"))})
        if path == "/guardian/config":
            return self._send(200, config_overview())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        body = {}
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n:
                body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            body = {}
        if path == "/guardian/test/mock-check":
            ci = scheduler.trigger_mock_check(result=body.get("result"))
            return self._send(200, {"ok": True, "checkin": ci})
        if path == "/guardian/test/telegram":
            ok, detail = telegram_notify.send(
                body.get("text") or "🛡️ Guardian test message — Telegram is wired up correctly.")
            return self._send(200, {"ok": ok, "detail": detail})
        return self._send(404, {"error": "not found"})


def main():
    storage.init_db()
    scheduler.start()
    telegram_notify.send("🛡️ Guardian service started — wellness checks are scheduled.")
    server = ThreadingHTTPServer(("127.0.0.1", settings.port), Handler)
    print(f"[guardian] listening on 127.0.0.1:{settings.port} · provider={settings.call_provider} "
          f"· schedule={settings.schedule}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
