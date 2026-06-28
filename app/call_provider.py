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
    def __init__(self, status, error=None, provider="mock", extra=None):
        self.status = status      # answered | missed | failed
        self.error = error
        self.provider = provider
        # Optional side-channel for extra answers gathered in the same call beyond the
        # wellness status — e.g. {"second_digit": "1"} for the Monday trash-day rider.
        self.extra = extra or {}


class CallProvider:
    name = "base"

    def place_call(self, target_type, target_value, message=None, second_question=None, primary=None) -> CallResult:
        raise NotImplementedError


# Optional override set by the /guardian/test/mock-check endpoint, e.g. force "missed".
_mock_override = {"result": None}


def set_mock_result(result):
    _mock_override["result"] = result


class MockProvider(CallProvider):
    name = "mock"

    def place_call(self, target_type, target_value, message=None, second_question=None, primary=None) -> CallResult:
        valid = ("confirmed_ok", "needs_darcee", "answered_unconfirmed", "missed", "failed")
        result = _mock_override["result"] or settings.mock_result
        if result == "answered":          # legacy alias → a confirmed wellness pass
            result = "confirmed_ok"
        if result == "random":
            result = random.choice(["confirmed_ok", "confirmed_ok", "needs_darcee",
                                    "answered_unconfirmed", "missed", "failed"])
        if result not in valid:
            result = "confirmed_ok"
        # ANGEL-14: a primary-override (standalone trash) call returns the raw pressed digit.
        # Simulate it from mock_second_digit so the standalone flow can be exercised. A
        # mock "missed"/"failed" stays a no-answer (no digit captured).
        if primary:
            if result in ("missed", "failed"):
                return CallResult(result, provider="mock", extra={})
            sd = str(settings.mock_second_digit)
            return CallResult("answered_question" if sd else "answered_unconfirmed",
                              provider="mock", extra={"primary_digit": sd})
        # Simulate the trash-day answer so the rider can be exercised without a real call.
        extra = {"second_digit": settings.mock_second_digit} if second_question else None
        return CallResult(result, provider="mock", extra=extra)


class TelnyxProvider(CallProvider):
    """Scaffold for Telnyx Programmable Voice (outbound call + webhook events).
    Phase 1: NOT wired — returns a safe 'failed' so no real call is placed."""
    name = "telnyx"

    def place_call(self, target_type, target_value, message=None, second_question=None) -> CallResult:
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

    def place_call(self, target_type, target_value, message=None, second_question=None, primary=None) -> CallResult:
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
        #
        # ANGEL-14: a `primary` override turns this into a single custom question (e.g. the
        # standalone trash yes/no) instead of the wellness menu — same generic voice-app
        # mechanism, just different message/digits/acks. Wellness calls (primary=None) are
        # completely unchanged.
        call_msg = message or settings.call_message
        reprompt_msg = settings.call_reprompt
        p_accept = [settings.okay_digit, settings.needs_call_digit]
        p_confirm_digit = settings.okay_digit
        p_ack = {settings.okay_digit: settings.ack_okay, settings.needs_call_digit: settings.ack_needs_call}
        if primary:
            call_msg = primary.get("message") or call_msg
            reprompt_msg = primary.get("reprompt") or reprompt_msg
            p_accept = primary.get("accept_digits") or p_accept
            p_confirm_digit = primary.get("confirm_digit") or p_accept[0]
            p_ack = primary.get("ack") or {}
        # ANGEL-13: when voice is enabled, tell Mom she may speak (additive wording;
        # the DTMF instructions stay intact). DTMF remains primary. The wellness "menu"
        # suffix is wellness-specific, so it's skipped for a primary-override question.
        if settings.voice_fallback_enabled:
            if not primary:
                call_msg = (call_msg + " " + settings.voice_menu_suffix).strip()
            reprompt_msg = (reprompt_msg + " " + settings.voice_reprompt_suffix).strip()
        payload = {
            "to": str(target_value),
            "message": call_msg,
            "mode": "announce",
            "device": settings.angel_device,
            "timeoutSeconds": settings.ring_timeout,
            "confirm": settings.confirm_enabled,        # collect a key press
            "confirmDigit": p_confirm_digit,
            "acceptDigits": p_accept,
            "confirmWindow1Ms": settings.confirm_window1_ms,  # 15s before the re-prompt
            "confirmReprompt": reprompt_msg,            # spoken if no key in the first window
            "confirmAck": p_ack,                        # audible ack spoken for the pressed key
        }
        # ANGEL-13: enable the voice path on the voice-app for this call. Speech maps
        # to the SAME digit, so the read-back below is unchanged. Phrase overrides are
        # optional — when empty, the voice-app uses its vetted built-in lists.
        if settings.voice_fallback_enabled:
            payload["voiceFallback"] = True
            if primary:
                if primary.get("voice_groups"):
                    payload["voiceGroups"] = primary["voice_groups"]
            else:
                okay_ph = settings.voice_okay_phrases
                needs_ph = settings.voice_needs_phrases
                # Override the full phrase set only if BOTH branches are configured;
                # otherwise the voice-app uses its vetted built-in lists for both.
                if okay_ph and needs_ph:
                    payload["voiceGroups"] = [
                        {"digit": settings.okay_digit, "phrases": okay_ph},
                        {"digit": settings.needs_call_digit, "phrases": needs_ph},
                    ]
        # Optional Monday trash-day rider: a second DTMF question asked in the same call.
        # second_question = {"message","accept_digits","yes_digit","reprompt","ack"}.
        if second_question:
            sq_msg = second_question["message"]
            if settings.voice_fallback_enabled:
                sq_msg_suffix = settings.voice_trash_suffix
                if sq_msg_suffix:
                    sq_msg = (sq_msg + " " + sq_msg_suffix).strip()
            sq = {
                "message": sq_msg,
                "acceptDigits": second_question["accept_digits"],
                "yesDigit": second_question["yes_digit"],
                "window1Ms": settings.confirm_window1_ms,
                "reprompt": second_question.get("reprompt"),
                "confirmAck": second_question.get("ack"),
            }
            # ANGEL-13: yes/no voice for the trash rider (DTMF primary). Phrase
            # overrides only if BOTH configured; else voice-app uses its built-ins.
            if settings.voice_fallback_enabled:
                yes_ph = settings.voice_trash_yes_phrases
                no_ph = settings.voice_trash_no_phrases
                accept = second_question["accept_digits"]
                no_digit = accept[1] if len(accept) > 1 else "2"
                if yes_ph and no_ph:
                    sq["voiceGroups"] = [
                        {"digit": second_question["yes_digit"], "phrases": yes_ph},
                        {"digit": no_digit, "phrases": no_ph},
                    ]
            payload["secondQuestion"] = sq
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
        second_digit = ""       # trash-day rider: which digit Mom pressed for question 2
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
            sd = st.get("secondDigit")
            if sd not in (None, ""):
                second_digit = str(sd)
            if st.get("failureReason"):
                reason = st.get("failureReason")
            state = (st.get("state") or "").lower()
            if state in ("completed", "failed", "ended"):
                break

        # ANGEL-05 outcome model — a connected call is NOT "Mom is okay".
        # She must press a menu key: 1 = okay, 2 = have Darcee call. No input = unconfirmed.
        if answered:
            # ANGEL-14: a primary-override call reports the raw pressed digit; the caller
            # (the standalone trash runner) maps it to yes/no — no wellness semantics.
            if primary:
                return CallResult("answered_question" if digit else "answered_unconfirmed",
                                  provider="3cx", extra={"primary_digit": digit})
            extra = {"second_digit": second_digit} if second_question else None
            if digit == settings.needs_call_digit:
                return CallResult("needs_darcee", provider="3cx", extra=extra)
            if digit == settings.okay_digit or confirmed:
                return CallResult("confirmed_ok", provider="3cx", extra=extra)
            return CallResult("answered_unconfirmed", provider="3cx", extra=extra)
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
