"""
Call provider abstraction so Guardian isn't married to one telephony backend.

Phase 1: the MOCK provider is the only one that actually "runs" — it simulates
answered/missed/failed so the scheduler, retries, logging and Telegram can all be
tested without placing a single real call.

Telnyx and 3CX are scaffolded with the same interface but are DISABLED until their
credentials are present AND CALL_PROVIDER is switched away from "mock". They will
never place a real call in Phase 1 with the default config.
"""
import random

from app.config import settings


class CallResult:
    def __init__(self, status, error=None, provider="mock"):
        self.status = status      # answered | missed | failed
        self.error = error
        self.provider = provider


class CallProvider:
    name = "base"

    def place_call(self, target_type, target_value) -> CallResult:
        raise NotImplementedError


# Optional override set by the /guardian/test/mock-check endpoint, e.g. force "missed".
_mock_override = {"result": None}


def set_mock_result(result):
    _mock_override["result"] = result


class MockProvider(CallProvider):
    name = "mock"

    def place_call(self, target_type, target_value) -> CallResult:
        result = _mock_override["result"] or settings.mock_result
        if result == "random":
            result = random.choice(["answered", "answered", "missed", "failed"])
        if result not in ("answered", "missed", "failed"):
            result = "answered"
        return CallResult(result, provider="mock")


class TelnyxProvider(CallProvider):
    """Scaffold for Telnyx Programmable Voice (outbound call + webhook events).
    Phase 1: NOT wired — returns a safe 'failed' so no real call is placed."""
    name = "telnyx"

    def place_call(self, target_type, target_value) -> CallResult:
        if not (settings.telnyx_api_key and settings.telnyx_connection_id and settings.telnyx_from):
            return CallResult("failed", error="telnyx_not_configured", provider="telnyx")
        # Phase 2: POST https://api.telnyx.com/v2/calls then resolve answered/missed
        # via the call.answered / call.hangup webhooks. Intentionally not placing a
        # real call in Phase 1.
        return CallResult("failed", error="telnyx_not_implemented_phase1", provider="telnyx")


class ThreeCXProvider(CallProvider):
    """Places real calls through the shared Claude-Phone voice-app outbound API,
    registered as Angel's 3CX extension (same plumbing Max uses). Dials Mom's
    extension (internal) or her cell (external) and reports answered/missed by
    watching the call's answeredAt."""
    name = "3cx"

    def place_call(self, target_type, target_value, message=None) -> CallResult:
        import json
        import time as _time
        import urllib.request
        base = settings.voice_app_url.rstrip("/")
        if not base:
            return CallResult("failed", error="voice_app_url_not_set", provider="3cx")
        payload = {
            "to": str(target_value),
            "message": message or settings.call_message,
            "mode": "announce",
            "device": settings.angel_device,
            "timeoutSeconds": settings.ring_timeout,
        }
        try:
            req = urllib.request.Request(base + "/api/outbound-call",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                started = json.loads(r.read().decode())
            call_id = started.get("callId")
            if not started.get("success") or not call_id:
                return CallResult("failed", error=str(started)[:120], provider="3cx")
        except Exception as e:
            return CallResult("failed", error="dial_failed:" + str(e)[:100], provider="3cx")

        # poll for outcome
        deadline = _time.time() + settings.ring_timeout + 20
        answered = False
        while _time.time() < deadline:
            _time.sleep(3)
            try:
                with urllib.request.urlopen(base + "/api/call/" + call_id, timeout=10) as r:
                    st = json.loads(r.read().decode())
            except Exception:
                continue
            if st.get("answeredAt"):
                answered = True
            if st.get("state") in ("completed", "failed", "ended"):
                break
        return CallResult("answered" if answered else "missed", provider="3cx")


def get_provider() -> CallProvider:
    return {"mock": MockProvider, "telnyx": TelnyxProvider, "3cx": ThreeCXProvider}.get(
        settings.call_provider, MockProvider)()
