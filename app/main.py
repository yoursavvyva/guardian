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
    good = (CheckinStatus.ANSWERED, CheckinStatus.MANUALLY_CONFIRMED_OK)
    missed_today = sum(1 for c in today if c["final_status"] in (CheckinStatus.MISSED, CheckinStatus.ESCALATED))
    needs_darcee_today = sum(1 for c in today if c["final_status"] == CheckinStatus.NEEDS_DARCEE)
    escalation_active = any(c["final_status"] == CheckinStatus.ESCALATED for c in today)
    # overall today rollup for the dashboard dot. needs_darcee = attention (yellow), NOT red.
    if escalation_active:
        today_status = "red"
    elif any(c["final_status"] == CheckinStatus.PENDING for c in today):
        today_status = "yellow"
    elif needs_darcee_today:
        today_status = "yellow"
    elif today and all(c["final_status"] in good for c in today):
        today_status = "green"
    elif today:
        today_status = "yellow"
    else:
        today_status = "idle"
    # Outstanding call-back requests (any day) Darcee hasn't acknowledged yet.
    pending = storage.unacked_needs_darcee()
    return {
        "today_status": today_status,
        "last_check_status": last["final_status"] if last else None,
        "last_answered_at": answered["scheduled_time"] if answered else None,
        "missed_checks_today": missed_today,
        "needs_darcee_today": needs_darcee_today,
        "pending_callbacks": [
            {"id": c["id"], "scheduled_time": c["scheduled_time"], "reminder_count": c.get("reminder_count") or 0}
            for c in pending
        ],
        "escalation_active": escalation_active,
        "next_check_at": scheduler.next_scheduled_check(),
        # ANGEL-08: most recent inbound call-back from Mom (reporting surface).
        "last_callback_time": storage.get_meta("last_callback_time"),
        "last_callback_outcome": storage.get_meta("last_callback_outcome"),
        # ANGEL-10: whether today's remaining scheduled checks are paused.
        "paused_today": scheduler.is_paused_today(),
        # ANGEL-12: Monday trash-day answer (does the trash go out tomorrow?).
        "trash": scheduler.trash_status(),
    }


def checkins_today():
    s, e, now = _today_bounds()
    today = storage.checkins_between(s, e, limit=50)
    sched = scheduler._scheduled_today(now)
    completed = [c for c in today if c["final_status"] in (CheckinStatus.ANSWERED, CheckinStatus.MANUALLY_CONFIRMED_OK)]
    missed = [c for c in today if c["final_status"] in (CheckinStatus.MISSED, CheckinStatus.ESCALATED)]
    needs_darcee = [c for c in today if c["final_status"] == CheckinStatus.NEEDS_DARCEE]
    pending = [c for c in today if c["final_status"] == CheckinStatus.PENDING]
    return {
        "scheduled": [d.strftime("%-I:%M %p") for d in sched],
        "completed": len(completed),
        "missed": len(missed),
        "needs_darcee": len(needs_darcee),
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


# ---- ANGEL-09b: direct Alexa custom-endpoint (replaces the Alexa-hosted Lambda) ----
# Guardian answers Alexa itself — no second network hop / Lambda cold-start, so no timeout.
# Spoken lines mirror the old lambda; Mom hears a clear confirmation instead of an error.
ALEXA_LAUNCH = ("Hi Mom, it's Angel, just checking in. If you're okay, say: I'm okay. "
                "Or, if you'd like a call, say: call me.")
ALEXA_OKAY = "Thank you, Mom. I'm glad you're okay. Have a wonderful day."
ALEXA_NEEDS = "Thank you, Mom. I'll let Darcee know you'd like a call. Talk to you later."
ALEXA_HELP = "Say: I'm okay. Or say: call me."
ALEXA_BYE = "Okay, Mom. Take care."
ALEXA_ERROR = ("I'm sorry, something went wrong on my end. "
               "Please try again, or wait for Angel's phone call.")


def _alexa_say(text, end=True, reprompt=None):
    """Build a valid Alexa response envelope."""
    resp = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if reprompt:
        resp["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": resp}


def _alexa_app_id(body):
    for keys in (("context", "System", "application", "applicationId"),
                 ("session", "application", "applicationId")):
        cur = body
        try:
            for k in keys:
                cur = cur[k]
            return cur
        except (KeyError, TypeError):
            continue
    return None


def handle_alexa_skill(body):
    """Parse an Alexa request and return an Alexa response dict. OkayIntent / NeedDarceeIntent
    map to scheduler.handle_alexa_wellness (same reconcile + Telegram as the token route)."""
    req = (body or {}).get("request") or {}
    rtype = req.get("type")
    if rtype == "LaunchRequest":
        return _alexa_say(ALEXA_LAUNCH, end=False, reprompt=ALEXA_HELP)
    if rtype == "SessionEndedRequest":
        return _alexa_say("", end=True)
    if rtype == "IntentRequest":
        name = ((req.get("intent") or {}).get("name")) or ""
        if name == "OkayIntent":
            out = scheduler.handle_alexa_wellness("okay")
            return _alexa_say(ALEXA_OKAY if out.get("ok") else ALEXA_ERROR)
        if name == "NeedDarceeIntent":
            out = scheduler.handle_alexa_wellness("needs_darcee")
            return _alexa_say(ALEXA_NEEDS if out.get("ok") else ALEXA_ERROR)
        if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent"):
            return _alexa_say(ALEXA_BYE)
        # Help / NavigateHome / anything unrecognized → re-prompt, keep listening.
        return _alexa_say(ALEXA_HELP, end=False, reprompt=ALEXA_HELP)
    return _alexa_say(ALEXA_BYE)


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
        if path == "/guardian/audit":
            q = self._qs()
            return self._send(200, {"audit": storage.recent_audit(int(q.get("limit", 20)))})
        if path == "/guardian/config":
            return self._send(200, config_overview())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = {}
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n:
                body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            body = {}
        # ANGEL-09: the Alexa skill route authenticates with its OWN token (it is
        # public-facing), kept separate from the admin token used by everything else.
        if path == "/guardian/alexa/wellness":
            tok = settings.alexa_token
            if not tok:
                return self._send(503, {"ok": False, "error": "alexa channel not configured"})
            if self.headers.get("X-Guardian-Alexa-Token") != tok:
                return self._send(401, {"ok": False, "error": "unauthorized"})
            out = scheduler.handle_alexa_wellness(body.get("intent"))
            return self._send(200 if out.get("ok") else 400, out)
        if path == "/guardian/alexa/skill":
            # ANGEL-09b: direct Alexa custom-endpoint. Alexa cannot send our shared token,
            # so the auth boundary is the skill's applicationId (when configured). Always
            # returns 200 with a valid Alexa envelope so Alexa speaks a real reply.
            want = settings.alexa_skill_id
            if want and _alexa_app_id(body) != want:
                return self._send(403, {"error": "forbidden"})
            return self._send(200, handle_alexa_skill(body))
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if path == "/guardian/acknowledge":
            # Mark a needs_darcee call-back as done (Darcee called Mom). Stops reminders.
            # Body: {"checkin_id": N}; if omitted, acknowledges the oldest un-acked one.
            cid = body.get("checkin_id")
            if cid is None:
                pending = storage.unacked_needs_darcee()
                if not pending:
                    return self._send(200, {"ok": True, "nothing_pending": True})
                cid = pending[0]["id"]
            ci, _changed = scheduler.acknowledge_and_confirm(cid, by=str(body.get("by") or "darcee"))
            if not ci:
                return self._send(404, {"ok": False, "error": "checkin not found"})
            acked = bool(ci.get("acknowledged"))
            return self._send(200, {"ok": acked, "acknowledged": acked, "checkin": ci})
        if path == "/guardian/inbound/wellness":
            # ANGEL-08: the voice-app POSTs here when Mom CALLS Angel back and runs the
            # press-1/press-2 wellness menu. Body: {caller, digit, outcome, source}.
            out = scheduler.handle_inbound_callback(
                caller=body.get("caller"), digit=body.get("digit"), outcome=body.get("outcome"))
            return self._send(200, {"ok": True, **out})
        if path == "/guardian/inbound/followup":
            # ANGEL-08 add-on: a call-back follow-up answer (e.g. the recovered Monday
            # trash question). Body: {checkin_id, key, digit}. Tracked separately from
            # the wellness outcome — never changes the wellness status.
            out = scheduler.record_callback_followup(
                body.get("checkin_id"), body.get("key"), body.get("digit"))
            return self._send(200, {**out})
        if path == "/guardian/manual-confirm":
            # ANGEL-10: resolve a specific check-in as manually OK (PMC button parity).
            cid = body.get("checkin_id")
            if cid is None:
                return self._send(400, {"error": "checkin_id required"})
            ci, changed = scheduler.manual_confirm_ok(
                int(cid), by=str(body.get("by") or "darcee"), chat=body.get("chat"))
            return self._send(200, {"ok": changed, "changed": changed, "checkin": ci})
        if path == "/guardian/pause":
            scheduler.pause_today(by=str(body.get("by") or "darcee"), chat=body.get("chat"))
            return self._send(200, {"ok": True, "paused_today": True})
        if path == "/guardian/resume":
            was = scheduler.resume_checks(by=str(body.get("by") or "darcee"), chat=body.get("chat"))
            return self._send(200, {"ok": True, "was_paused": was, "paused_today": False})
        if path == "/guardian/test/mock-check":
            ci = scheduler.trigger_mock_check(result=body.get("result"))
            return self._send(200, {"ok": True, "checkin": ci})
        if path == "/guardian/test/telegram":
            ok, detail = telegram_notify.send(
                body.get("text") or "🛡️ Guardian test message — Telegram is wired up correctly.")
            return self._send(200, {"ok": ok, "detail": detail})
        if path == "/guardian/test/call":
            # Places ONE REAL call via the voice-app (as Angel) to the given number,
            # regardless of CALL_PROVIDER. Use this to test against YOUR number first.
            to = (body.get("to") or "").strip()
            if not to:
                return self._send(400, {"error": "provide 'to' (a number or extension to test-call)"})
            from app.call_provider import ThreeCXProvider
            from app.config import mask_phone
            telegram_notify.send(f"🧪 Angel: placing a TEST call to {mask_phone(to)}…")
            res = ThreeCXProvider().place_call("test", to, message=body.get("message"))
            telegram_notify.send(f"🧪 Angel test call → {res.status}{(' (' + res.error + ')') if res.error else ''}.")
            return self._send(200, {"ok": res.status == "confirmed_ok", "status": res.status, "error": res.error})
        return self._send(404, {"error": "not found"})


def main():
    storage.init_db()
    scheduler.start()
    from app import telegram_listener  # Angel-bot callback listener (ack taps); separate from Max
    telegram_listener.start()
    telegram_notify.send("🛡️ Guardian service started — wellness checks are scheduled.")
    server = ThreadingHTTPServer(("127.0.0.1", settings.port), Handler)
    print(f"[guardian] listening on 127.0.0.1:{settings.port} · provider={settings.call_provider} "
          f"· schedule={settings.schedule}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
