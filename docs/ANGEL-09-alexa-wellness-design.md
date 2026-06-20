# ANGEL-09 — Alexa Wellness Channel (DESIGN ONLY — not built)

_Design report, 2026-06-19. No code written. Guardian + Angel phone workflows
remain exactly as they are today (see [CONFIG.md](../CONFIG.md))._

## Goal

Make **Alexa the easiest at-home way for Mom to answer her wellness check**, while
the **Angel phone calls stay the away-from-home / fallback channel** and **Guardian
stays the system of record**. Driven by real pilot feedback: Mom is sharp and
independent, dislikes feeling like a bother, and sometimes can't reach the phone
fast enough — but she has an **Echo Show 11** and an **Echo Dot** at home and
naturally *wants* a low-friction way to respond.

---

## 1. Recommended architecture (hybrid)

Two halves, each using the simplest reliable tool for its job:

```
  HOME — PRIMARY (hands-free)                         AWAY / FALLBACK (unchanged)
  ───────────────────────────                         ───────────────────────────
  12:00 PM / 8:00 PM                                   12:15 PM / 8:15 PM  (= scheduled + grace)
  ┌──────────────────────────────┐                    ┌────────────────────────────────┐
  │ Alexa scheduled ROUTINE        │                   │ Guardian phone leg (TODAY's     │
  │  → Announcement on Show + Dot  │                   │  ladder, 100% unchanged):       │
  │  "Angel is checking in.        │   no confirm      │  ext 39514 → 20m → ext → 20m →  │
  │   Are you okay today?          │ ───in grace──────▶│  cell → Telegram escalation     │
  │   Say: Alexa, tell Angel I'm   │     window        └────────────────────────────────┘
  │   okay  /  I need Darcee"      │
  └──────────────┬─────────────────┘
     voice  OR   │  tap (Echo Show 11 on-screen buttons)
                 ▼
  ┌──────────────────────────────┐
  │ Alexa Custom Skill "Angel"     │  invocation name = "Angel"
  │  OkayIntent / NeedDarceeIntent │  (Alexa-hosted Lambda — free)
  └──────────────┬─────────────────┘
                 ▼  HTTPS POST + bearer token
  ┌──────────────────────────────────────────────┐
  │ Guardian public route /guardian/alexa/wellness │  (nginx → 127.0.0.1:8101)
  │  → handle_inbound_callback(source='alexa')      │  ← REUSES the ANGEL-08 handler
  └──────────────┬─────────────────────────────────┘
                 ▼
  ┌──────────────────────────────────────────────┐
  │ Guardian — SYSTEM OF RECORD                     │
  │  okay        → satisfy today's check +          │
  │                cancel the pending phone leg      │
  │  need Darcee → needs_darcee + reminder loop      │
  │  either way  → Telegram to Darcee               │
  └──────────────────────────────────────────────┘
```

**Why this split:**
- **Outbound prompt = a native scheduled Alexa Routine.** Alexa can speak a custom
  Announcement on a schedule with **zero backend**. The prompt text is static, so it
  never needs to be dynamic. Lowest-maintenance possible.
- **Inbound response = a custom Skill named "Angel."** The phrase *"Alexa, tell Angel
  I'm okay"* is literally the Alexa one-shot skill grammar (`tell <skill> <intent>`),
  so the desired wording maps 1:1 to a skill — no awkward rephrasing.
- **Guardian already has the brain.** ANGEL-08 added `handle_inbound_callback()` +
  the `source` column + reconcile-and-cancel logic. Alexa is just a **third source**
  (`phone-callback` → `alexa`) feeding the same proven path.
- **Fallback is the existing phone ladder, untouched.** We only *delay its start* by a
  grace window when Alexa is enabled. If Mom confirms on Alexa first, the phone leg is
  already satisfied and never dials. If she doesn't, the phone workflow runs exactly as
  it does today.

### The grace-window state machine (the one real new idea in Guardian)

```
12:00  check-in created → state "awaiting_response"
       phone first-attempt DEFERRED to 12:00 + GRACE (default 15 min)
         │
         ├─ Alexa "I'm okay"  (before 12:15) → ANSWERED, phone leg cancelled.  ✅ no call placed
         ├─ Alexa "need Darcee"               → NEEDS_DARCEE + Telegram.       ✅ no call placed
         └─ nothing by 12:15                  → existing phone ladder fires.   ☎️ unchanged behavior
```

This reuses the existing `next_attempt_at` retry machinery — the phone leg is just a
"retry" scheduled for `scheduled_time + grace` instead of firing at `scheduled_time`.

---

## 2. What Alexa would say

**12:00 PM and 8:00 PM (identical), Announcement on Echo Show 11 + Echo Dot:**

> "Hello Mom. Angel is checking in. Are you okay today?
> You can say: *Alexa, tell Angel I'm okay.*
> Or: *Alexa, tell Angel I need Darcee.*"

On the **Echo Show 11**, the skill can also paint two big on-screen buttons (an APL
visual): **🟢 "I'm OK"** and **🟡 "I need Darcee"** — so Mom can **tap** instead of
speak. For an independent, sharp user this is often *more* reliable than voice and
needs no enunciation.

**Acknowledgements Alexa speaks back:**
- After okay: *"Wonderful. I've let Darcee know you're doing okay. Have a lovely day, Mom."*
- After need-Darcee: *"Okay, I've let Darcee know you'd like a call. She'll reach out soon."*

---

## 3. What Mom would say

| Mom says | Skill intent | Guardian result |
|---|---|---|
| "Alexa, tell Angel **I'm okay**" | `OkayIntent` | wellness **confirmed**, today's check **satisfied**, pending phone leg **cancelled**, 💚 Telegram |
| "Alexa, tell Angel **I need Darcee**" | `NeedDarceeIntent` | **needs_darcee** recorded, 🟡 Telegram + reminder loop (taps "I called her" to clear) |
| *(taps 🟢 / 🟡 on Echo Show)* | same intents | same as above |
| *(says nothing within the grace window)* | — | **phone workflow takes over**, unchanged |

Natural-language variants the skill should also accept (Alexa sample utterances):
"I'm fine", "I'm good", "all good", "doing well" → Okay; "call Darcee", "I need my
daughter", "have Darcee call me" → NeedDarcee.

---

## 4. Guardian changes required

All small and additive; nothing in the existing phone path changes behaviour.

1. **New public route** `POST /guardian/alexa/wellness`
   - Body: `{ "intent": "okay" | "needs_darcee", "device"?: "show"|"dot" }`.
   - Auth: bearer token `GUARDIAN_ALEXA_TOKEN` (separate from the localhost phone token).
   - Maps intent → outcome and calls the **existing** `scheduler.handle_inbound_callback(caller="alexa", outcome=..., source="alexa")`.
2. **`source="alexa"`** — the `guardian_checkins.source` column already exists (ANGEL-08).
   Add `alexa` as a recognised value + Telegram wording ("…via Alexa").
3. **Grace-window deferral mode** (config-gated):
   - `GUARDIAN_ALEXA_ENABLED=true`
   - `GUARDIAN_ALEXA_GRACE_MINUTES=15`
   - When enabled, `_tick()` creates the scheduled check in an `awaiting_response`
     state with `next_attempt_at = scheduled + grace` (defer the first phone attempt)
     instead of dialing at `scheduled_time`. Everything after the grace point is the
     current ladder.
4. **Reporting** — extend the existing `last_callback_*` meta with a channel tag
   (`alexa` vs `phone`), and surface "awaiting Alexa" / "answered via Alexa" on the PMC
   widget + `/guardian` page. (Builds on the ANGEL-08 reporting fields.)
5. **nginx** — one locked-down HTTPS path → `127.0.0.1:8101` (per the VPS new-site
   guide: `listen 31.220.96.150:443 ssl`, behind Cloudflare). e.g. `angel.poppysuite.com/alexa`.

> No change to the dialing ladder, retry timing, escalation, or the ANGEL-08 phone
> call-back. Alexa is purely an earlier, easier on-ramp to the same record.

---

## 5. Security model

**Darcee's hard rules are structurally guaranteed — not just configured:**
- **No automatic Drop In, no automatic camera, no automatic video.** None of the
  proposed components (scheduled Routine, custom Skill, Guardian endpoint) have or
  request Drop In / camera / video permissions. There is **no API surface** in this
  design that *can* initiate them. Drop In and camera viewing stay **manual, in
  Darcee's own Alexa app, decided only by Darcee.** This design cannot change that.

**Endpoint hardening (the one new attack surface — a public inbound route):**
- HTTPS only; long random bearer token (`GUARDIAN_ALEXA_TOKEN`); Cloudflare in front;
  rate-limited; narrow path that does **one** benign thing.
- With an **Alexa-hosted** skill, Amazon verifies the request *to the Lambda*; the
  Lambda then calls Guardian with the shared secret. (If instead Guardian were the
  direct skill endpoint, it would additionally perform full **Alexa request-signature
  verification** — cert chain + timestamp. Alexa-hosted avoids needing that in Guardian.)
- **Least privilege / blast radius:** the worst a leaked token can do is record a false
  "okay" or "needs Darcee" — it cannot read data, cannot place calls, cannot reach
  camera/Drop In. Guardian remains system of record with the phone fallback, so a
  spurious "okay" that wasn't really Mom is the only failure mode, and the next
  scheduled check re-verifies.
- **No account linking** needed (single fixed Mom + single Guardian), so **no personal
  data** is exchanged or stored by the skill.
- **No health details** leave the system (existing Guardian rule); Telegram stays
  okay/needs-call only.
- **Voice privacy:** Amazon retains Alexa voice history by default; Darcee can set
  Mom's account to **not save recordings** / **auto-delete**. The skill itself stores
  nothing.

---

## 6. Approaches evaluated

| # | Approach | What it is | Maps "tell Angel I'm okay"? | Backend | 3rd-party dep | Pros | Cons |
|---|---|---|---|---|---|---|---|
| **1** | **Custom Alexa Skill** ⭐ (inbound) | Skill "Angel", `OkayIntent`/`NeedDarceeIntent`, Alexa-hosted Lambda → Guardian | **Yes, exactly** | Free Lambda | None | Natural wording; touch buttons on Show; robust intents; no monthly cost | Skill setup; Mom's Echos must be same Amazon account *or* beta-test enrolled; needs public Guardian endpoint |
| **2** | **Routine + webhook** (Voice Monkey / Home Assistant / IFTTT) | Custom utterance ("Alexa, I'm okay") triggers a Routine → webhook → Guardian | Partly — phrasing becomes "Alexa, I'm okay" (Routines can't use `tell <skill>`) | None (or self-host) | **Yes** (Voice Monkey or HA) | No skill/cert; fast to stand up | Adds a SaaS/self-host dependency; less natural phrase; 2 routines; webhook reliability tied to 3rd party |
| **3** | **Alexa "custom action" / Proactive Notifications** | Skill variant: custom actions + Notifications/Proactive Events API for the outbound nudge | Yes (still a skill) | Free Lambda | None | Tighter outbound control; could push notifications | More complex (customer opt-in, Proactive Events); overkill — a scheduled Routine already announces |
| **4** | **Hybrid (recommended): #1 skill for inbound + native scheduled Routine for the announcement + Guardian grace window** | Best tool per half | **Yes** | Free Lambda | None | Lowest maintenance overall; natural wording; zero outbound backend; reuses ANGEL-08; safe phone fallback | Schedule lives in **two** places (Alexa Routine *and* Guardian `.env`) — must update both if times change |

**Recommendation: Approach 4 (hybrid).** Use the **custom skill** for Mom's responses
(natural "tell Angel…" wording, plus Show touch buttons) and a **native scheduled
Routine** for the spoken prompt (no server). Add the **grace-window deferral** so the
phone leg only fires if Alexa goes unanswered.

> **Optional upgrade (only if Darcee later wants one source of truth for timing):** have
> Guardian *trigger* the announcement itself (via Voice Monkey or Home Assistant
> `notify.alexa_media`) at 12:00/8:00 instead of a native Routine. Then the schedule
> lives only in Guardian and prompts could be dynamic. Cost: one more moving part. Not
> needed for v1.

---

## 7. Complexity

| Piece | Effort | Notes |
|---|---|---|
| Native scheduled Routine (announcement) | **Trivial** | Configured in the Alexa app; no code |
| Custom skill "Angel" (2 intents + APL buttons) | **Moderate** | Manifest, intents, Alexa-hosted Lambda calling Guardian |
| Guardian `/guardian/alexa/wellness` route | **Small** | Thin wrapper over existing `handle_inbound_callback` |
| Grace-window deferral | **Small–moderate** | Defer `next_attempt_at`; new soft state + config |
| nginx public path | **Small** | Existing new-site pattern |
| Mom's device enrollment (same account or beta invite) | **Small, one-time** | Decision/prereq, see §9 |

---

## 8. Cost

| Item | Cost |
|---|---|
| Alexa-hosted skill (Lambda + storage) | **$0** — Amazon's free skill tier |
| Native Alexa Routines | **$0** |
| Guardian endpoint (nginx/Cloudflare on existing VPS) | **$0** |
| Voice Monkey (only if Approach 2 / optional upgrade) | $0 free tier, ~$2–5/mo paid |
| Per-check runtime cost | **$0** |
| **Net effect on phone cost** | **Reduces it** — every Alexa confirmation avoids a paid/again-dialed phone leg |

---

## 9. Reliability

**Strong, because the phone fallback makes Alexa best-effort, not load-bearing:**
- **Announcement:** native, high reliability. Caveats: device volume, Do-Not-Disturb,
  or a powered-off Echo could mute it → grace window expires → phone takes over. (Set
  Mom's devices to not DND during 12/8; the Show stays plugged in.)
- **Inbound recognition:** "Angel" is a clean invocation name; intents are simple.
  Misrecognition or "skill not enabled" → no confirm → phone fallback. The **Echo Show
  touch buttons** remove voice-recognition risk entirely for the on-screen path.
- **Network/endpoint down:** Lambda→Guardian POST fails → no confirm recorded → phone
  fallback. Guardian never *loses* a check; absence of an Alexa confirm simply means the
  existing ladder runs.
- **Whole-system guarantee:** Guardian is the system of record and the phone ladder is
  unchanged, so **no single Alexa failure can cause a missed wellness check** — it can
  only cause a phone call that would have happened anyway.

**Key tuning:** `GUARDIAN_ALEXA_GRACE_MINUTES`. Long enough for Mom to respond hands-free
(she's not racing to a phone — 10–15 min is generous), short enough that a genuine
problem still triggers the phone leg promptly.

**One prerequisite decision:** which Amazon account owns Mom's Echo Show 11 + Dot?
- Same account as Darcee's Alexa developer account → skill works in **development mode**
  directly.
- Mom's own account → enroll her via the **Skill Beta Testing** program (free, up to 500
  testers, no public certification) — a one-time email invite she accepts.

---

## 10. Pros and cons — at a glance

**Pros of adding the Alexa channel**
- Solves the real problem: hands-free, no rushing to the phone.
- Feels low-burden to Mom — a quick "I'm okay" to a device already in her kitchen/living room.
- Touch buttons on the Echo Show for the most reliable, no-speech path.
- Reuses ANGEL-08 — minimal new Guardian surface.
- Likely **reduces** phone/Telnyx usage.
- Fully honours the no-Drop-In / no-camera rules by construction.

**Cons / watch-items**
- New public inbound endpoint = one new (well-contained) attack surface.
- Schedule maintained in two places (Alexa app + Guardian) unless the optional
  Guardian-triggered-announcement upgrade is taken.
- Device-account enrollment is a one-time setup wrinkle.
- Alexa voice history is retained by Amazon unless Mom's account is set to auto-delete.

---

## 11. Suggested build order (when Darcee says go — NOT now)

1. Confirm Mom's Echo account → pick same-account vs beta-enroll.
2. Native scheduled Routines (12:00 + 8:00 announcements) — provides immediate value with zero code.
3. Custom skill "Angel" (Alexa-hosted) with Okay/NeedDarcee intents + Show APL buttons.
4. Guardian: `/guardian/alexa/wellness` route + `source='alexa'` wording (thin, reuses ANGEL-08).
5. Guardian: grace-window deferral + config + PMC "awaiting Alexa / answered via Alexa" surfacing.
6. nginx locked path + token; end-to-end test against Darcee's own Echo first, then Mom's.
7. Tune `GUARDIAN_ALEXA_GRACE_MINUTES`; keep phone fallback verified.

_End of design. Nothing in this document has been implemented._
