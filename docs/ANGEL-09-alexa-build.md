# ANGEL-09 — Alexa wellness channel (BUILD increment 1)

_Built 2026-06-19. Scope per Darcee: **Guardian route + skill skeleton + docs only.**
Alexa Routine setup stays **manual instructions** until tested. Phone behaviour is
**unchanged**; Max/Judy untouched; **no camera / Drop In automation**._

## What was built (live now)
- **Guardian route** `POST /guardian/alexa/wellness` — public-facing, authenticated with
  its **own** token `X-Guardian-Alexa-Token` (separate from the admin token).
  Body `{intent:"okay"|"needs_darcee"}`.
- **`scheduler.handle_alexa_wellness(intent)`** — reuses the **ANGEL-08 reconcile**:
  - `okay` → records an `source='alexa'`, ANSWERED check + **satisfies/cancels today's
    pending phone check** (cancels pending retries). 💚 Telegram: "Alexa Check-In … Mom told Alexa she's okay".
  - `needs_darcee` → `source='alexa'` NEEDS_DARCEE row + 🟡 Telegram + the existing
    "I called her" ack/reminder loop.
  - Logged to the audit table; `last_callback_outcome` = `alexa_confirmed_ok` / `alexa_needs_darcee`.
- **Skill skeleton** in `alexa-skill/` (`interaction-model.json`, `lambda/index.js`, `README.md`).
- Reconcile tightened to only ever flip **scheduled-origin** checks (never alexa/inbound/telegram rows).

## Telegram wording per channel (requirement #5)
| Channel | Confirmation message starts with |
|---|---|
| Phone (scheduled, pressed 1) | "💚 Guardian: Mom confirmed she's okay (pressed 1) on the … check" |
| Phone call-back (Mom dialed Angel) | "💚 Angel Call-Back … Mom called Angel back" |
| **Alexa** | "💚 Alexa Check-In … Mom told Alexa she's okay" |

## Manual setup — Alexa announcements (do in the Alexa app, not code)
Create two **scheduled Routines** so Alexa speaks the prompt at 12:00 PM and 8:00 PM on
Mom's Echo Show 11 + Echo Dot:
1. Alexa app → **More → Routines → +**.
2. **When**: Schedule → 12:00 PM (and a second routine for 8:00 PM), daily.
3. **Action**: Alexa Says → **Announcement** (or Custom):
   > "Hello Mom. Angel is checking in. Are you okay today?
   > You can say: Alexa, tell Angel I'm okay. Or: Alexa, tell Angel I need Darcee."
4. **From**: select Mom's Echo Show 11 and Echo Dot.

_(The announcement is intentionally decoupled from Guardian for zero-maintenance. Keep
the Routine times in sync with `GUARDIAN_SCHEDULE`.)_

## Deploy / enable the route
1. Set a token in `~/projects/guardian/.env`:
   `GUARDIAN_ALEXA_TOKEN=<long-random>` → `pm2 restart guardian-assistant`.
2. Expose the route over HTTPS (nginx, per the VPS new-site guide:
   `listen 31.220.96.150:443 ssl`, behind Cloudflare), proxying **only**
   `/guardian/alexa/wellness` → `127.0.0.1:8101`. e.g. `angel.poppysuite.com`.
3. Put the same URL + token in the Alexa-hosted skill (see `alexa-skill/README.md`).
4. Test "tell Angel I'm okay" against **Darcee's** device first → expect the 💚 Telegram.

## Amazon account decision
Use whichever account owns Mom's Echos:
- **Mom's account** → free **Skill Beta** invite to her email.
- **Darcee's/dev account** → **dev mode** (works directly).

## Phase 2 (NOT enabled yet — keeps phone behaviour unchanged)
The "Alexa-first, phone only if no confirmation in **15 min**" deferral is **config-gated
and OFF by default**:
- `GUARDIAN_ALEXA_ENABLED=false` (default) → phone fires at the scheduled time exactly as
  today. Until this is flipped on (after live Alexa testing), the only Alexa effect is that
  a confirmation **cancels remaining phone retries** via reconcile.
- When ready: set `GUARDIAN_ALEXA_ENABLED=true` + `GUARDIAN_ALEXA_GRACE_MINUTES=15`; the
  scheduler will defer the phone first-attempt by the grace window so Alexa can answer first.
  (Scheduler deferral code lands in increment 2.)

## Security
- Separate `X-Guardian-Alexa-Token`; route does one benign thing (record okay/needs_darcee).
  Worst case of a leaked token = a false okay/needs_darcee, re-verified by the next check.
- **No Drop In / camera / video** anywhere in the skill, route, or Guardian. Nothing here
  can initiate them; those stay manual in Darcee's Alexa app.
- No account linking, no personal data, single fixed Mom + Guardian.

## Unchanged
Phone dialing ladder, scheduled-check timing, Max/Judy, and the trash/call-back flows.
