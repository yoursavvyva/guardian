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

    def place_call(self, target_type, target_value, message=None) -> CallResult:
        valid = ("confirmed_ok", "needs_darcee", "answered_unconfirmed", "missed", "failed")
        result = _mock_override["result"] or settings.mock_result
        if result == "answered":          # legacy alias → a confirmed wellness pass
            result = "confirmed_ok"
        if result == "random":
            result = random.choice(["confirmed_ok", "confirmed_ok", "needs_darcee",
                                    "answered_unconfirmed", "missed", "failed"])
        if result not in valid:
            result = "confirmed_ok"
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
        # ANGEL-05 two-choice wellness menu over the voice-app's confirm flow:
        # mode=announce + confirm collects a DTMF press; acceptDigits=[1,2] ends the
        # wait early on either key. The voice-app reports the pressed key back as
        # "confirmDigit" on GET /api/call/:id (and "confirmed" if it equals okay_digit).
        payload = {
            "to": str(target_value),
            "message": message or settings.call_message,
            "mode": "announce",
            "device": settings.angel_device,
            "timeoutSeconds": settings.ring_timeout,
            "confirm": settings.confirm_enabled,        # collect a key press
            "confirmDigit": settings.okay_digit,        # "okay" key -> confirmed=true
            "acceptDigits": [settings.okay_digit, settings.needs_call_digit],
            "confirmReprompt": settings.call_reprompt,  # spoken if no key in the first window
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
        confirmed = False
        digit = ""              # ANGEL-05: which menu digit Mom pressed
        reason = None
        state = None
        while _time.time() < deadline:
            _time.sleep(3)
            try:
                with urllib.request.urlopen(base + "/api/call/" + call_id, timeout=10) as r:
                    resp = json.loads(r.read().decode())
                st = resp.get("data", resp)  # status route wraps fields under "data"
            except Exception:
                continue
            if st.get("answeredAt"):
                answered = True
            if st.get("confirmed"):
                confirmed = True
            # Voice-app reports the pressed key as "confirmDigit"; "digit" kept as a fallback.
            pressed = st.get("confirmDigit")
            if pressed in (None, ""):
                pressed = st.get("digit")
            if pressed not in (None, ""):
                digit = str(pressed)
            if st.get("failureReason"):
                reason = st.get("failureReason")
            state = (st.get("state") or "").lower()
            if state in ("completed", "failed", "ended"):
                break

        # ANGEL-05 outcome model — a connected call is NOT "Mom is okay".
        # She must press a menu key: 1 = okay, 2 = have Darcee call. No input = unconfirmed.
        if answered:
            if digit == settings.needs_call_digit:
                return CallResult("needs_darcee", provider="3cx")
            if digit == settings.okay_digit or confirmed:
                return CallResult("confirmed_ok", provider="3cx")
            return CallResult("answered_unconfirmed", provider="3cx")
        # Not answered: genuine no-answer vs TECHNICAL failure.
        no_answer = {"no_answer", "no-answer", "busy", "timeout", "local_hangup", "remote_hangup"}
        r = (reason or "").lower()
        if r and r not in no_answer:
            return CallResult("failed", error=reason, provider="3cx")  # technical — NOT "Mom missed"
        if not r and state == "failed":
            return CallResult("failed", error="call_failed", provider="3cx")
        return CallResult("missed", provider="3cx")


def get_provider() -> CallProvider:
    return {"mock": MockProvider, "telnyx": TelnyxProvider, "3cx": ThreeCXProvider}.get(
        settings.call_provider, MockProvider)()
