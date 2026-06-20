# ANGEL-10 — Telegram Control Buttons (not open commands)

_Built 2026-06-19. Adds safe, auditable Telegram controls to the Angel bot
(`momsguardianangel_bot`). **Angel is not Max** — it exposes only explicit safety
**buttons**, never open-ended command execution, and no text ever triggers a call._

## How Darcee opens the controls
- Send **`/menu`** (or `/start`, `/angel`, `/panel`, `/controls`) → Angel replies with
  the inline control panel.
- Send **`/status`** → read-only status text.
- **Any other text is silently ignored.** There is no command parser, no free-form
  actions, and no way for text to place a call.

All real actions happen via inline buttons, authorized to Darcee's chat only, and every
mutating tap is logged (`guardian_audit` table: action, actor, chat, checkin_id, ts).

## The control panel

| Button | Action | Safety |
|---|---|---|
| ✅ **Mom is OK** | Resolve the active check-in → `manually_confirmed_ok`, `source=telegram_darcee`, clear retries/escalation | Button carries the **checkin_id**; only resolves a still-open (pending/escalated/missed) check. A stale button can't flip a newer or already-resolved check-in. |
| 📞 **I called Mom** | Clear the `needs_darcee` reminder loop (Darcee called back) | Carries checkin_id; existing ANGEL-06 ack path |
| ☎️ **Call Mom now** | Place one on-demand Angel wellness call to Mom | **Two-tap confirm** ("Yes, call now / Cancel") before any real dial |
| ⏸ **Pause checks today** | Stop today's *remaining* scheduled checks from starting | In-progress checks finish; auto-resumes tomorrow |
| ▶️ **Resume checks** | Re-enable the schedule if paused | Shown in place of Pause when paused |
| 📊 **Status** | Read-only Guardian/Angel status | No state change |

## Staleness guarantee
Every check-specific button embeds its `checkin_id` at render time. Taps are resolved
**by that id only** — so an old panel's "Mom is OK" can only ever target its own
(by-then resolved) check, and **never** the current/newer one. Resolving requires the
target to still be open; otherwise the tap is a no-op with a "no longer active" reply.

## Audit
`storage.add_audit(action, actor, chat_id, checkin_id, detail)` records every
mom_is_ok / call_now / pause_today / resume tap (who + when), also printed to the PM2
log and queryable via `GET /guardian/audit?limit=N`.

## API parity (for the PMC page; same scheduler functions the buttons call)
- `POST /guardian/manual-confirm` `{checkin_id, by}`
- `POST /guardian/pause` / `POST /guardian/resume`
- `GET  /guardian/status` now includes `paused_today`
- `GET  /guardian/audit`

## New status
`manually_confirmed_ok` is treated like `answered` for the daily rollup (counts as a
completed/good check; never as missed/escalated).

## Out of scope (unchanged this phase)
- Alexa plan (ANGEL-09) — untouched.
- Max / Judy — untouched.
- Phone call scripts / dialing ladder — untouched.
