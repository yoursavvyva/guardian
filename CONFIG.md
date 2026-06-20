# Guardian / Angel — Current Configuration

_Last verified: 2026-06-19 against the live service (`GET /guardian/config` on
`127.0.0.1:8101`), not just the `.env` file._

| Setting | Value | Source |
|---|---|---|
| **Schedule** | **12:00 PM + 8:00 PM** (America/New_York) | `GUARDIAN_SCHEDULE=12:00,20:00` |
| **Provider** | **3cx** (real calls — live) | `CALL_PROVIDER=3cx` |
| **Escalation** | **Telegram** (Angel bot `momsguardianangel_bot`, chat 8688027239) | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` set; `telegram_configured: true` |
| **Callback (inbound)** | **Enabled** — Mom dials Angel ext 39515 back | `inboundMode: "wellness"` on Angel in `devices.json`; `GUARDIAN_WEBHOOK_URL` reachable in voice-app |

## Details

### Schedule — 12:00 PM + 8:00 PM
Two daily wellness checks. `GUARDIAN_START_DATE=2026-06-19` (first real call day).
Timezone `America/New_York`.

### Provider — 3cx (LIVE, real calls)
- Calls go through the shared Claude-Phone voice-app as **Angel (ext 39515)**.
- Mom's destination: **3CX ext 39514** (attempts 1 & 2), then **cell +1 336-706-7766** (attempt 3).
- Retry ladder: `GUARDIAN_MAX_ATTEMPTS=3`, `GUARDIAN_RETRY_MINUTES=20` (ext → 20 min → ext → 20 min → cell → escalate).
- Telnyx is **not** the path for Mom (`telnyx_configured: false`); cell fallback dials via 3CX's trunk.

### Escalation — Telegram
- All alerts go to Telegram (no ntfy). Outbound: 💚 confirmed / 🟡 needs-Darcee / ⚠️ retry / 🚨 escalation.
- needs_darcee items re-nudge every 30 min (`GUARDIAN_ACK_REMINDER_MINUTES`), quiet 9pm–8am, until you tap "✅ I called her."

### Callback — Enabled (ANGEL-08, inbound)
- Mom calls **ext 39515** back → Angel runs the same **press 1 = okay / press 2 = call me** menu.
- **Only press 1** satisfies the day's check and cancels pending retry/escalation.
- **Only press 2** creates a needs_darcee request (enters the reminder loop).
- **No key pressed** → `callback_no_response`: notify-only (🔔), satisfies nothing.
- Keypad windows are generous (20s, then re-prompt + 12s) so Mom has time to open the dialer.
- `last_callback_time` / `last_callback_outcome` are stored and shown in `/guardian/status`.

## How to re-check anytime
```bash
TOKEN=$(grep '^GUARDIAN_API_TOKEN=' ~/projects/guardian/.env | cut -d= -f2)
curl -s http://127.0.0.1:8101/guardian/config -H "X-Guardian-Token: $TOKEN" | python3 -m json.tool
```
