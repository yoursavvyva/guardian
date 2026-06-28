"""
Guardian configuration. Loads from guardian/.env (simple parser, no external deps).
Everything is env-driven so nothing sensitive is hardcoded or committed.
"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # guardian/
_ENV_CACHE = None


def _load_env():
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE
    env = {}
    path = os.path.join(HERE, ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    _ENV_CACHE = env
    return env


def env(key, default=None):
    return os.environ.get(key) or _load_env().get(key) or default


class Settings:
    @property
    def timezone(self):
        return env("GUARDIAN_TIMEZONE", "America/New_York")

    @property
    def schedule(self):
        # Pilot schedule: "11:00,18:00" -> ["11:00","18:00"] (11:00 AM + 6:00 PM).
        # An explicitly-empty GUARDIAN_SCHEDULE means DISABLED (no scheduled checks) —
        # do NOT fall back to the default in that case (that's the scheduler kill-switch).
        raw = os.environ.get("GUARDIAN_SCHEDULE")
        if raw is None:
            raw = _load_env().get("GUARDIAN_SCHEDULE")
        if raw is None:
            raw = "11:00,18:00"
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def start_date(self):
        # Optional go-live date (YYYY-MM-DD, local tz). No scheduled checks fire on a
        # date before this — lets us arm the schedule now but begin calls on a later day.
        raw = (env("GUARDIAN_START_DATE", "") or "").strip()
        if not raw:
            return None
        try:
            from datetime import date
            y, m, d = [int(x) for x in raw.split("-")]
            return date(y, m, d)
        except (ValueError, TypeError):
            return None

    @property
    def retry_minutes(self):
        try:
            return int(env("GUARDIAN_RETRY_MINUTES", "20"))
        except ValueError:
            return 20

    @property
    def max_attempts(self):
        try:
            return int(env("GUARDIAN_MAX_ATTEMPTS", "3"))
        except ValueError:
            return 3

    @property
    def alexa_grace_minutes(self):
        # Option B (Alexa-preferred at home): when a check-in window opens, Guardian
        # waits this many minutes for Mom to confirm via Alexa ("Alexa, tell Guardian
        # Angel I'm okay") BEFORE Angel places the fallback phone call. An Alexa
        # confirmation during (or just before) the window cancels the call entirely.
        # Set to 0 to call immediately (legacy call-first behavior).
        try:
            return max(0, int(env("GUARDIAN_ALEXA_GRACE_MINUTES", "15")))
        except ValueError:
            return 15

    @property
    def port(self):
        try:
            return int(env("GUARDIAN_PORT", "8101"))
        except ValueError:
            return 8101

    @property
    def api_token(self):
        return env("GUARDIAN_API_TOKEN", "")

    # ---- telegram ----
    @property
    def telegram_token(self):
        return env("TELEGRAM_BOT_TOKEN", "")

    @property
    def telegram_chat_id(self):
        return env("TELEGRAM_CHAT_ID", "")

    # ---- call provider ----
    @property
    def call_provider(self):
        return env("CALL_PROVIDER", "mock").lower()

    @property
    def mock_result(self):
        # answered | missed | failed | random
        return env("GUARDIAN_MOCK_RESULT", "answered").lower()

    @property
    def mom_extension(self):
        return env("MOM_3CX_EXTENSION", "")

    @property
    def mom_cell(self):
        return env("MOM_CELL_PHONE", "")

    # ---- telnyx ----
    @property
    def telnyx_api_key(self):
        return env("TELNYX_API_KEY", "")

    @property
    def telnyx_connection_id(self):
        return env("TELNYX_CONNECTION_ID", "")

    @property
    def telnyx_from(self):
        return env("TELNYX_FROM_NUMBER", "")

    # ---- 3cx ----
    # Guardian calls FROM Angel's own extension (SIP creds below). Mom's ext is
    # only a dial destination (MOM_3CX_EXTENSION) — her SIP creds are never needed.
    @property
    def threecx_base_url(self):
        return env("THREECX_BASE_URL", "")

    @property
    def threecx_extension(self):
        return env("THREECX_EXTENSION", "")

    @property
    def threecx_auth_id(self):
        return env("THREECX_AUTH_ID", "")

    @property
    def threecx_password(self):
        return env("THREECX_PASSWORD", "")

    @property
    def threecx_sip_domain(self):
        return env("THREECX_SIP_DOMAIN", "")

    @property
    def threecx_sip_port(self):
        try:
            return int(env("THREECX_SIP_PORT", "5060"))
        except ValueError:
            return 5060

    @property
    def threecx_outbound_proxy(self):
        # The local 3CX SBC (Docker `3cxsbc`) that Angel registers/sends SIP through;
        # it tunnels to the 3CX cloud. Same path Max/Claude Phone uses.
        return env("THREECX_OUTBOUND_PROXY", "")

    # ---- voice-app (shared Claude-Phone outbound-call API; Angel placed real calls here) ----
    @property
    def voice_app_url(self):
        return env("VOICE_APP_URL", "http://127.0.0.1:3030")

    @property
    def angel_device(self):
        # Device name/extension registered in the voice-app's devices.json.
        return env("ANGEL_DEVICE", "Angel")

    @property
    def call_message(self):
        # ANGEL-05: two-choice wellness menu (DTMF only — no STT, no open-ended Q&A).
        return env("GUARDIAN_CALL_MESSAGE",
                   "Hi Mom, this is Angel checking in. Are you okay today? "
                   "Press 1 for yes. Press 2 if you need Darcee to call you.")

    @property
    def call_reprompt(self):
        # Re-prompt spoken when no digit is received in the first window.
        return env("GUARDIAN_CALL_REPROMPT",
                   "I didn't receive your answer. Press 1 if you are okay. "
                   "Press 2 if you need Darcee to call you.")

    @property
    def ack_okay(self):
        # Spoken right after Mom presses 1, before hangup (audible confirmation).
        return env("GUARDIAN_ACK_OKAY",
                   "Thank you, Mom. I'm glad you're okay. Have a wonderful day.")

    @property
    def ack_needs_call(self):
        # Spoken right after Mom presses 2, before hangup (audible confirmation).
        return env("GUARDIAN_ACK_NEEDS_CALL",
                   "Thank you, Mom. I'll let Darcee know you'd like a call. Talk to you later.")

    @property
    def confirm_enabled(self):
        return env("GUARDIAN_CONFIRM", "true").lower() != "false"

    @property
    def confirm_digit(self):
        # Legacy alias; the menu now uses okay_digit/needs_call_digit.
        return env("GUARDIAN_CONFIRM_DIGIT", "1")

    @property
    def okay_digit(self):
        # Press this = "I'm okay" -> confirmed_ok. Defaults to the legacy confirm digit.
        return env("GUARDIAN_OKAY_DIGIT", self.confirm_digit)

    @property
    def needs_call_digit(self):
        # Press this = "have Darcee call me" -> needs_darcee.
        return env("GUARDIAN_NEEDS_CALL_DIGIT", "2")

    # ---- ANGEL-13: voice fallback (say "I'm okay" / "I need Darcee") ----
    # Additive to DTMF; DTMF always wins. Default OFF — flip on only after testing.
    @property
    def voice_fallback_enabled(self):
        return env("GUARDIAN_VOICE_FALLBACK_ENABLED", "false").lower() == "true"

    @staticmethod
    def _csv(raw):
        return [s.strip() for s in str(raw or "").split(",") if s.strip()]

    @property
    def voice_okay_phrases(self):
        # Optional override; empty -> voice-app uses its vetted built-in list.
        return self._csv(env("GUARDIAN_VOICE_OKAY_PHRASES", ""))

    @property
    def voice_needs_phrases(self):
        return self._csv(env("GUARDIAN_VOICE_NEEDS_PHRASES", ""))

    @property
    def voice_trash_yes_phrases(self):
        return self._csv(env("GUARDIAN_VOICE_TRASH_YES_PHRASES", ""))

    @property
    def voice_trash_no_phrases(self):
        return self._csv(env("GUARDIAN_VOICE_TRASH_NO_PHRASES", ""))

    @property
    def voice_menu_suffix(self):
        # Appended to the wellness prompt when voice is enabled, so Mom is told
        # she can speak. Keeps the DTMF wording intact and just adds the option.
        # "call me" is name-free => most reliable for speech recognition.
        return env("GUARDIAN_VOICE_MENU_SUFFIX",
                   "Or, you can just say: I'm okay. Or, to have me call Darcee, say: call me.")

    @property
    def voice_reprompt_suffix(self):
        return env("GUARDIAN_VOICE_REPROMPT_SUFFIX",
                   "You can press a button, or just say: I'm okay, or say: call me.")

    @property
    def voice_trash_suffix(self):
        return env("GUARDIAN_VOICE_TRASH_SUFFIX", "You can also just say yes or no.")

    @property
    def ring_timeout(self):
        try:
            return int(env("GUARDIAN_RING_SECONDS", "30"))
        except ValueError:
            return 30

    @property
    def confirm_window1_ms(self):
        # First listening window after the menu (ms) before the re-prompt. 15s gives
        # Mom time to open the keypad + press so she usually won't hear the re-prompt.
        try:
            return int(env("GUARDIAN_CONFIRM_WINDOW1_MS", "15000"))
        except ValueError:
            return 15000

    # ---- ANGEL-06: call-back reminders (when Mom presses 2 / needs_darcee) ----
    @property
    def ack_reminder_minutes(self):
        # How often to re-nudge Darcee until she acknowledges calling Mom back.
        try:
            return int(env("GUARDIAN_ACK_REMINDER_MINUTES", "30"))
        except ValueError:
            return 30

    @property
    def quiet_start_hour(self):
        # No reminders at/after this local hour (24h). 21 = 9pm.
        try:
            return int(env("GUARDIAN_QUIET_START", "21"))
        except ValueError:
            return 21

    @property
    def quiet_end_hour(self):
        # Reminders resume at/after this local hour. 8 = 8am.
        try:
            return int(env("GUARDIAN_QUIET_END", "8"))
        except ValueError:
            return 8

    # ---- Trash-day rider: a 2nd question asked only on the configured day's check ----
    @property
    def trash_enabled(self):
        return env("GUARDIAN_TRASH_ENABLED", "true").lower() != "false"

    @property
    def trash_day(self):
        # Day-of-week the trash question rides along (full English name, e.g. "Monday").
        return env("GUARDIAN_TRASH_DAY", "Monday")

    @property
    def trash_time(self):
        # Which scheduled check carries it (HH:MM — must match a GUARDIAN_SCHEDULE entry).
        return env("GUARDIAN_TRASH_TIME", "12:00")

    @property
    def trash_pickup_day(self):
        # The ACTUAL garbage pickup day (full English name, e.g. "Tuesday"). The question +
        # all alerts name this day so the wording is correct even when Mom is asked days
        # ahead (e.g. asked Sunday for a Tuesday pickup). Empty = fall back to "tomorrow".
        return env("GUARDIAN_TRASH_PICKUP_DAY", "").strip()

    @property
    def _trash_when(self):
        # Phrase for when the trash goes out: "on Tuesday" if a pickup day is set, else "tomorrow".
        d = self.trash_pickup_day
        return ("on " + d) if d else "tomorrow"

    @property
    def trash_message(self):
        return env("GUARDIAN_TRASH_MESSAGE",
                   f"One more thing, Mom. Does the trash need to go out {self._trash_when}? "
                   "Press 1 for yes. Press 2 for no.")

    @property
    def trash_reprompt(self):
        return env("GUARDIAN_TRASH_REPROMPT",
                   f"I didn't catch that. If the trash needs to go out {self._trash_when}, press 1. "
                   "If not, press 2.")

    @property
    def trash_yes_digit(self):
        return env("GUARDIAN_TRASH_YES_DIGIT", "1")

    @property
    def trash_no_digit(self):
        return env("GUARDIAN_TRASH_NO_DIGIT", "2")

    @property
    def trash_ack_yes(self):
        return env("GUARDIAN_TRASH_ACK_YES",
                   f"Thank you, Mom. I'll let Darcee know the trash needs to go out {self._trash_when}.")

    @property
    def trash_ack_no(self):
        return env("GUARDIAN_TRASH_ACK_NO",
                   f"Okay, Mom. No trash {self._trash_when}. Thank you.")

    @property
    def trash_extra_chat_ids(self):
        # Additional Telegram chat IDs (besides TELEGRAM_CHAT_ID) to alert on a "yes" —
        # e.g. Darcee's sister. Comma-separated. Empty until she sets up her account.
        raw = env("GUARDIAN_TRASH_CHAT_IDS", "") or ""
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def mock_second_digit(self):
        # Test-only: which digit the mock provider simulates for the trash question.
        return env("GUARDIAN_MOCK_SECOND_DIGIT", "1")

    # ---- ANGEL-14: STANDALONE trash sequence (its own call, separate from wellness) ----
    @property
    def trash_standalone(self):
        # TRUE: the trash question is NOT a rider on the noon wellness call. Instead, AFTER
        # the noon wellness sequence fully finishes, Guardian runs a SEPARATE trash sequence:
        # Alexa grace window -> ONE Angel call -> callback window -> Darcee yes/no buttons.
        # Default FALSE keeps the legacy rider behaviour until explicitly enabled (safe rollout).
        return env("GUARDIAN_TRASH_STANDALONE", "false").lower() == "true"

    @property
    def trash_standalone_message(self):
        # Primary question for the standalone trash CALL (greets Mom, then asks). Distinct from
        # trash_message, which is phrased as a "one more thing" rider after the wellness menu.
        return env("GUARDIAN_TRASH_STANDALONE_MESSAGE",
                   "Hi Mom, it's Angel with one quick question. Does the trash need to go out "
                   f"{self._trash_when}? Press 1 for yes. Press 2 for no.")

    @property
    def trash_standalone_reprompt(self):
        return env("GUARDIAN_TRASH_STANDALONE_REPROMPT",
                   f"I didn't catch that. If the trash needs to go out {self._trash_when}, press 1. "
                   "If not, press 2.")

    @property
    def trash_callback_window_minutes(self):
        # After Angel's single trash call goes unanswered, how long to wait for Mom to call
        # back (or use Alexa) before alerting Darcee with yes/no buttons to follow up herself.
        try:
            return int(env("GUARDIAN_TRASH_CALLBACK_WINDOW_MINUTES", "30"))
        except ValueError:
            return 30

    @property
    def trash_ack_reminder_minutes(self):
        # Re-nudge the SISTER (trash_extra_chat_ids only — never Darcee) this often until she
        # taps "Got it" on a YES (trash-goes-out) answer, or the pickup day passes. NO re-nudges
        # for "no" (nothing to do). 0 = no re-nudge. Quiet overnight.
        try:
            return int(env("GUARDIAN_TRASH_ACK_REMINDER_MINUTES", "120"))
        except ValueError:
            return 120

    @property
    def trash_escalate_deadline_time(self):
        # Hard deadline: if the sister still hasn't confirmed a YES by this time on the day
        # BEFORE pickup (e.g. Monday 12:00 for a Tuesday pickup), escalate to Darcee ONCE so she
        # has time to make sure it goes out. Empty = no deadline escalation.
        return env("GUARDIAN_TRASH_ESCALATE_DEADLINE_TIME", "12:00").strip()

    # ---- ANGEL-09: Alexa wellness channel ----
    @property
    def alexa_token(self):
        # Shared secret the Alexa skill (Lambda) sends as X-Guardian-Alexa-Token.
        # Kept SEPARATE from the admin api_token since the Alexa route is public-facing.
        return env("GUARDIAN_ALEXA_TOKEN", "")

    @property
    def alexa_skill_id(self):
        # ANGEL-09b (direct endpoint): the Alexa skill's applicationId
        # (amzn1.ask.skill.xxxx). When set, the /guardian/alexa/skill route ONLY accepts
        # requests carrying this id — that is the auth boundary for the public endpoint
        # (Alexa cannot send our shared token). Leave empty only for pre-skill testing.
        return env("GUARDIAN_ALEXA_SKILL_ID", "")

    @property
    def alexa_enabled(self):
        # Phase-2 grace-window deferral switch. DEFAULT FALSE = phone behaviour unchanged.
        # (The /guardian/alexa/wellness route + reconcile work regardless of this flag.)
        return env("GUARDIAN_ALEXA_ENABLED", "false").lower() == "true"

    @property
    def alexa_grace_minutes(self):
        # Phase-2: how long to wait for an Alexa confirmation before the phone leg fires.
        try:
            return int(env("GUARDIAN_ALEXA_GRACE_MINUTES", "15"))
        except ValueError:
            return 15

    @property
    def db_path(self):
        return env("GUARDIAN_DB", os.path.join(HERE, "data", "guardian.db"))


settings = Settings()


def mask_phone(value):
    """Mask a phone/extension for UI + logs: keep last 2-4 digits only."""
    if not value:
        return ""
    digits = "".join(c for c in str(value) if c.isdigit())
    if len(digits) <= 2:
        return "••"
    keep = digits[-4:] if len(digits) >= 6 else digits[-2:]
    return "•" * max(3, len(digits) - len(keep)) + " " + keep
