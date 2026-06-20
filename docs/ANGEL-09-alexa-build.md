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
2. Expose the route over HTTPS at **`angel.darceesellers.com`** (decided 2026-06-20),
   proxying **only** `/guardian/alexa/wellness` → `127.0.0.1:8101` (nginx per the VPS
   new-site guide: `listen 31.220.96.150:443 ssl`).
   - **DNS PREREQUISITE:** darceesellers.com is served from THIS VPS, and its DNS is at
     **Namecheap** (NS `dns1/dns2.registrar-servers.com`). Add an **A record
     Host=`angel` → 31.220.96.150**, TTL Automatic, in Namecheap → Domain List → Manage →
     **Advanced DNS**. The cert can't be issued until `angel.darceesellers.com` resolves.
   - Then nginx server block + certbot for `angel.darceesellers.com`; proxy only the one
     path, return 404 for everything else.
3. Put the same URL + token in the Alexa-hosted skill (see `alexa-skill/README.md`).
4. Test "tell Angel I'm okay" against **Darcee's** device first → expect the 💚 Telegram.

## Amazon account decision — DECIDED: Mom's own Amazon account
**Plan (Darcee, 2026-06-19): Mom's Echo Show 11 + Echo Dot stay on _Mom's own Amazon
account_.**
- **Angel skill** reaches her devices via a free **Skill Beta** invite to her Amazon
  email (no public certification; she taps the link to enable "Angel"). See
  `alexa-skill/README.md`.
- This also gives Mom a distinct Alexa **contact identity**, which is what makes
  Darcee's **manual Drop In / video / chat** work cleanly (see next section).

## Manual Drop In / video / chat (separate from Angel — Darcee-controlled)
This is **not** part of Angel/Guardian and is **never automated** — it's the normal Alexa
communications layer, initiated only by Darcee. With Mom on her own account:
1. In each Alexa app, add the other as a **contact** (phone-number based).
2. On Mom's devices: **enable Drop In** and **grant Darcee permission** (Settings →
   Communications → Drop In). Mom can revoke anytime — it's her control.
3. Darcee drops in via "Alexa, drop in on Mom" or the Alexa app.
   - **Video** Drop In works to the **Echo Show 11** (has a camera), from Darcee's Alexa
     app or her own Echo Show. The **Echo Dot is audio-only** (no camera).
- Angel's skill, route, and Guardian have **no** Drop In / camera / video permission and
  cannot initiate any of this; it stays manual and Mom-permissioned.

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
