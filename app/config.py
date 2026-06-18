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
        # "11:00,16:00,20:30" -> ["11:00","16:00","20:30"]
        raw = env("GUARDIAN_SCHEDULE", "11:00,16:00,20:30")
        return [s.strip() for s in raw.split(",") if s.strip()]

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
