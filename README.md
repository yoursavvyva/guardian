# Guardian — elderly wellness assistant (Phase 1)

Guardian calls Darcee's mother on a schedule, tracks whether she answered, retries,
and alerts Darcee on Telegram. It runs as its **own** service (PM2 `guardian-assistant`,
SQLite, internal HTTP API on `127.0.0.1:8101`) and exposes status to PMC through that
API — PMC never reads Guardian's database directly.

**Guardian is NOT Max.** It is completely separate. Max is untouched.

## Phase 1 scope
- Configurable schedule — pilot = **two daily checks: 11:00 AM + 6:00 PM** America/New_York.
- **Two-choice wellness menu (ANGEL-05, DTMF only — no speech recognition):**
  Angel asks *"Hi Mom, this is Angel checking in. Are you okay today? Press 1 for yes.
  Press 2 if you need Darcee to call you."* (re-prompts once if no input).
- Retry ladder: extension → wait 20m → extension → wait 20m → **cell** → escalate.
- Telegram notifications (started / calling / answered / **needs-Darcee** / missed+retry / escalation).
- **Mock call provider by default — no real calls are placed.** Telnyx & 3CX are
  scaffolded behind a provider interface but disabled until configured in Phase 2.

### Outcome model (ANGEL-05)
| Mom does | status | wellness_result | What happens |
|---|---|---|---|
| Presses **1** | `confirmed_ok` | `okay` | ✅ pass, no escalation |
| Presses **2** | `needs_darcee` | `needs_call` | 🟡 terminal (NOT a failure) — pings Darcee to call her |
| No input | `answered_unconfirmed` | — | retry / escalate |
| No answer | `missed` | — | retry / escalate |
| Technical error | `failed` | — | retry / escalate (never "Mom missed") |
- SQLite storage (`data/guardian.db`): `guardian_checkins`, `guardian_call_attempts`.
- Internal HTTP API for PMC (health/status/checkins/attempts/test), token-protected,
  phone numbers masked everywhere.

## Configure Telegram
1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the token.
2. Get your chat id (message the bot, then `https://api.telegram.org/bot<TOKEN>/getUpdates`).
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.
   (The shipped `.env` is pre-seeded with the existing PMC bot so tests work out of the box.)

## Run a mock test
```bash
# health (no auth)
curl -s 127.0.0.1:8101/guardian/health | python3 -m json.tool

# trigger a mock wellness check (force an outcome: answered|missed|failed)
curl -s -X POST 127.0.0.1:8101/guardian/test/mock-check \
  -H "X-Guardian-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"result":"missed"}'

# send a Telegram test
curl -s -X POST 127.0.0.1:8101/guardian/test/telegram -H "X-Guardian-Token: $TOKEN"
```
To watch a full **retry→escalation** cycle quickly: set `GUARDIAN_RETRY_MINUTES=0` and
`GUARDIAN_MOCK_RESULT=missed` in `.env`, restart, and trigger a mock check.

## Run with PM2
```bash
cd ~/projects/guardian
pm2 start ecosystem.config.js      # process name: guardian-assistant
pm2 save
pm2 logs guardian-assistant
```

## API (internal, 127.0.0.1 only, X-Guardian-Token header)
- `GET  /guardian/health` · `GET /guardian/status`
- `GET  /guardian/checkins?date_from&date_to&limit` · `GET /guardian/checkins/today`
- `GET  /guardian/attempts?checkin_id&date_from&date_to` · `GET /guardian/config`
- `POST /guardian/test/mock-check` · `POST /guardian/test/telegram`

## Safety (Phase 1)
- No 911, no auto-contacting siblings, does not replace the medical-alert button.
- No real calls unless `CALL_PROVIDER` is changed from `mock` **and** creds are set.
- No health details in Telegram; phone numbers masked in all UI/logs.

## Ready for Phase 2
Real Telnyx/3CX call placement + answer detection via webhooks; Alexa Show manual
check workflow; camera verification notes; AI call transcripts; voice provider
selection; family-notification escalation; manual "Mom is OK" acknowledgement.
