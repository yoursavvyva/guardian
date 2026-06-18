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
    """Scaffold for 3CX Call Control API outbound calls. Phase 1: NOT wired."""
    name = "3cx"

    def place_call(self, target_type, target_value) -> CallResult:
        if not (settings.threecx_extension and settings.threecx_auth_id and settings.threecx_password):
            return CallResult("failed", error="3cx_not_configured", provider="3cx")
        return CallResult("failed", error="3cx_not_implemented_phase1", provider="3cx")


def get_provider() -> CallProvider:
    return {"mock": MockProvider, "telnyx": TelnyxProvider, "3cx": ThreeCXProvider}.get(
        settings.call_provider, MockProvider)()
