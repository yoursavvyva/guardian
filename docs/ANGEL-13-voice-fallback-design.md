# ANGEL-13 — Voice-Response Fallback for Phone Wellness Calls

_Design report, 2026-06-23. **BUILD APPROVED + IMPLEMENTED 2026-06-23** — built,
unit-tested, and deployed **behind a default-OFF feature flag**. DTMF stays the
primary and authoritative input; this only **adds** a spoken path for the cases
where Mom has no keypad in front of her. Not enabled for Mom; awaiting Darcee's
supervised ext-39510 sign-off before activation._

> ## BUILD STATUS (2026-06-23)
> **LIVE for Mom as of 2026-06-23** after full Darcee-only ext-39510 validation
> (10 calls, inbound + outbound, all green). Enabled: Guardian
> `GUARDIAN_VOICE_FALLBACK_ENABLED=true` (outbound) + voice-app
> `VOICE_FALLBACK_ENABLED=true` (inbound). Validated on speakerphone (far-field
> proxy for the Echo); literal Echo-Dot path not separately tested but degrades
> safely. Live hardening added: force-finalize capture, transcript accumulation,
> name canonicalization + "call me" phrase, early-press capture + BARGE-IN.
>
> **Feature flags (default OFF — production behavior unchanged until flipped):**
> - **Outbound** wellness + trash rider: Guardian `GUARDIAN_VOICE_FALLBACK_ENABLED=true`
>   (Guardian then sends `voiceFallback` to the voice-app per call).
> - **Inbound** call-back (ext 39515): voice-app `VOICE_FALLBACK_ENABLED=true` in
>   `~/.claude-phone/.env` (inbound originates at the voice-app).
> - STT: voice-app `STT_*` (recommend Groq `whisper-large-v3`).
>
> **Files built:** voice-app `lib/speech-match.js` (new), `lib/voice-capture.js` (new),
> `lib/confirm.js` (+speech hook), `lib/outbound-routes.js` + `lib/inbound-wellness.js`
> (+capture wiring), `test/speech-match.test.js` (new), `test/confirm.test.js` (+5).
> Guardian `app/config.py` (+flags), `app/call_provider.py` (+payload), `.env.example`.
>
> **Tests:** speech-match **51/51**, confirm **12/12** (incl. DTMF-wins + ambiguous-reject),
> Guardian **46/46** (45 prior + 1 voice-payload). No regression.
>
> **Enablement instructions, deploy plan, and rollback plan are in §10–§11 below.**

## Motivation (the real pilot observation)

On check-in **#34** (2026-06-23, 12:00) Mom missed the Alexa grace window, Angel
correctly placed the phone fallback, and Mom **answered both extension attempts but
sent no DTMF** (`answered_unconfirmed` ×2); Darcee resolved it manually via Telegram.
Root cause: Mom's phone routes the **3CX app call's audio through her Echo**
(Bluetooth / hands-free), so she interacts with the Echo and never taps the 3CX
in-app keypad. The device-side mitigation (unpair Echo Bluetooth / force 3CX app
audio) is tracked separately. **This doc is the durable fix:** make the fallback
usable even when Mom answers hands-free — via Echo, a Bluetooth speaker, or a car —
by accepting a *simple spoken* "I'm okay" / "I need Darcee" **in addition to** the
keypad.

## Goal

Keep **DTMF as primary**, but allow simple spoken responses during Angel's phone
**fallback** and **call-back** flows:

- **Press 1 _or say_ "I'm okay"** → wellness confirmed (`confirmed_ok`)
- **Press 2 _or say_ "I need Darcee"** → call-back requested (`needs_darcee`)

A recognized phrase is mapped to **the same digit ('1'/'2')** the keypad would have
produced, so **every downstream Guardian behaviour is unchanged** — voice is just a
second way to generate a '1' or a '2'.

## Hard safety rules (non-negotiable)

1. **DTMF always wins.** Any keypress short-circuits speech, immediately and for the
   whole window.
2. **Never guess "okay."** A false "okay" is the only dangerous failure (it would
   suppress the phone ladder for a Mom who is *not* okay). The system must prefer a
   missed confirmation over a guessed one.
3. **Ambiguous / unclear / empty speech = no confirmation** → falls through to the
   existing retry + escalation ladder, exactly as a missed keypress does today.
4. **Require a clear phrase**, not a bare filler word. Accept e.g. "I'm okay",
   "I'm fine", "I'm alright", "all good"; and "I need Darcee", "I need my daughter".
   Bare "okay"/"yeah" alone is **not** sufficient for the okay branch (too easy to
   mishear / trigger on background speech).
5. **Telegram manual resolution stays the backstop** (ANGEL-10 "✅ Mom is OK" /
   "📞 I called Mom") — unchanged, always available.

## Explicitly NOT changed

- **Alexa-first flow** and the **15-minute grace window** (ANGEL-11) — untouched.
- **Existing DTMF behaviour** — the keypad path is byte-for-byte the same; voice is
  purely additive and feature-flagged off by default.
- **Telegram controls** (ANGEL-10) and the needs_darcee ack/reminder loop (ANGEL-06).
- **Max / Judy** and every other voice-app flow — gated so only Angel wellness legs
  opt in.
- Guardian's **outcome model, DB schema, reconcile logic, trash logic** — unchanged
  (speech resolves to a digit upstream of all of it).

---

## 1. Architecture

The components already exist; nothing new is added to the dependency set.

```
  Angel places wellness call (outbound fallback OR inbound call-back)
        │  (3CX → SBC → FreeSWITCH leg, as today)
        ▼
  voice-app plays the menu prompt (TTS, unchanged)
        │
        ├──────────────── LISTEN WINDOW (window1 → reprompt → window2) ───────────────┐
        │                                                                             │
        │   PRIMARY: DTMF          PARALLEL (additive): SPEECH                         │
        │   FreeSWITCH 'dtmf'      audio-fork (mod_audio_fork) → VAD utterance →       │
        │   events (confirm.js)    Whisper STT → matchWellnessSpeech(transcript)       │
        │        │                        │                                           │
        │        │ pressed 1/2            │ confident "I'm okay"/"I need Darcee"       │
        │        ▼                        ▼                                           │
        │   got = '1'|'2'  ◄── DTMF WINS ── speechDigit = '1'|'2' (only if no digit)  │
        └──────────────────────────────┬──────────────────────────────────────────────┘
                                        ▼
                resolved digit ('1' | '2' | none) — IDENTICAL to today's DTMF result
                                        ▼
        Guardian: handle_alexa/inbound/scheduled outcome paths — 100% UNCHANGED
```

**Key design choice — "speech → digit" adapter:** the speech path never invents a new
outcome. It produces the same `{confirmed, digit}` shape `collectConfirmation()`
returns today. So `outbound-session.js`, `ThreeCXProvider`, the scheduler, the DB,
reconcile, the trash rider, and Telegram all see exactly what they see now.

**Echo / barge-in handling:** the speech capture runs **only in the listen window,
after the prompt finishes playing** (the same windows DTMF uses). We do **not** fork
audio during TTS playback, so Angel's own voice is never transcribed.

**Turn order inside a window:**
1. Play menu prompt (TTS).
2. Open window1: DTMF listener (persistent) + start one audio-fork utterance capture.
3. If a digit arrives → resolve immediately (speech capture discarded).
4. Else if VAD yields an utterance → transcribe → `matchWellnessSpeech`:
   - clear match → set `speechDigit`, resolve.
   - null (ambiguous/unclear) → ignore, keep listening.
5. No resolution by end of window1 → play reprompt → window2 (same dual listen).
6. Still nothing → `{confirmed:false, digit:null}` → existing ladder continues.

---

## 2. Scope (where it applies)

| Flow | File today | Voice fallback? |
|---|---|---|
| **Outbound wellness fallback call** (12:00 / 20:00 ladder) | `voice-app/lib/outbound-routes.js` → `collectConfirmation` | **Yes** |
| **Inbound call-back** (Mom dials ext 39515) | `voice-app/lib/inbound-wellness.js` → `collectConfirmation` | **Yes** |
| **Monday trash rider** (second yes/no question) | `outbound-routes.js` `secondQuestion` + `inbound-wellness.js` followups | **Yes, same adapter** — "yes"/"no" → the configured yes/no digit; DTMF primary; identical safety rules. Trash is non-critical, so an unrecognized answer simply stays unknown (as today). |

All three reuse the **same** `matchWellnessSpeech` / `matchYesNo` helpers and the same
windowed collector — no per-flow special-casing beyond the keyword set.

---

## 3. Files likely touched

> All additive and feature-flagged. No file's existing behaviour changes when the
> flag is off.

**voice-app (`~/.claude-phone-cli/voice-app/`, built from this repo; container in `~/.claude-phone`):**

| File | Change |
|---|---|
| `lib/speech-match.js` **(new)** | Pure functions `matchWellnessSpeech(text) → '1'|'2'|null` and `matchYesNo(text, {yesDigit,noDigit}) → digit|null`. No I/O → trivially unit-testable. |
| `lib/confirm.js` | Extend `collectConfirmation` to optionally accept a speech result via an injected `listenForSpeech()` (dependency-injected, same pattern as `playMessage`). DTMF precedence preserved; default behaviour unchanged when no speech fn is passed. |
| `lib/outbound-routes.js` | When `voiceFallback:true`, wire an audio-fork utterance capture for the wellness leg + pass `listenForSpeech` into `collectConfirmation`. Reuses the already-injected `audioForkServer` + `whisperClient`. |
| `lib/inbound-wellness.js` | Same wiring for the call-back menu (and its trash follow-up). |
| `lib/audio-fork.js` | Likely no change — reuse the existing VAD/utterance capture; possibly a small helper to fork a single named leg for one utterance. |
| `test/speech-match.test.js` **(new)** | Truth-table unit tests (see §6). |
| `test/confirm.test.js` | Add cases: DTMF-beats-speech, speech-resolves-when-no-DTMF, ambiguous-speech-ignored. |

**Guardian (`~/projects/guardian/`):**

| File | Change |
|---|---|
| `app/call_provider.py` (`ThreeCXProvider`) | Send `voiceFallback: settings.voice_fallback_enabled` + the spoken-menu wording on wellness/inbound payloads. Read back the resolved digit **exactly as today** (`confirmDigit`/`secondDigit`). |
| `app/config.py` | New settings (see §4). |
| `app/scheduler.py` | Only the prompt/ack **wording** ("press one, or say I'm okay") — no logic change. |
| `.env.example` | Document the new flags. |
| `docs/ANGEL-13-voice-fallback-design.md` | This doc. |

**Untouched:** `main.py` Alexa routes, ANGEL-11 grace logic, ANGEL-10 Telegram
listener/controls, ANGEL-06 ack loop, Max/Judy device flows.

---

## 4. Config flags (all default OFF / no behaviour change until set)

Guardian `.env`:

| Flag | Default | Meaning |
|---|---|---|
| `GUARDIAN_VOICE_FALLBACK_ENABLED` | `false` | Master switch. Off → today's DTMF-only behaviour. |
| `GUARDIAN_VOICE_OKAY_PHRASES` | `i'm okay,i am okay,i'm fine,i'm alright,all good,i'm good` | Comma-list mapped to digit 1. |
| `GUARDIAN_VOICE_NEEDS_PHRASES` | `i need darcee,i need my daughter,call darcee,i need help` | Comma-list mapped to digit 2. |
| `GUARDIAN_VOICE_TRASH_YES_PHRASES` | `yes,yep,it does,put it out` | Trash rider → yes digit. |
| `GUARDIAN_VOICE_TRASH_NO_PHRASES` | `no,nope,it doesn't,skip it` | Trash rider → no digit. |
| `GUARDIAN_VOICE_MIN_CONFIDENCE` | `0.6` | Reject low-confidence transcripts (if the STT provider returns a score; else length/keyword heuristic). |

voice-app `~/.claude-phone/.env` (STT provider — reuses the existing `STT_*`):

| Var | Recommendation |
|---|---|
| `STT_API_KEY` / `STT_BASE_URL` / `STT_MODEL` | Point at **Groq** `whisper-large-v3` (`STT_BASE_URL=https://api.groq.com/openai/v1`, `STT_MODEL=whisper-large-v3`). |

---

## 5. STT provider recommendation

`voice-app/lib/whisper-client.js` is already OpenAI-compatible and honours `STT_*`
overrides (the code comment explicitly calls out Groq).

| Option | Cost | Latency | Notes |
|---|---|---|---|
| **Groq `whisper-large-v3`** ⭐ | ~free tier / fractions of a cent | ~0.3–0.8s | Recommended: cheap, fast, accurate; key already pattern-supported. |
| OpenAI `whisper-1` | ~$0.006/min | ~0.5–1s | Drop-in default if no Groq key. |
| Local FreeSWITCH `detect_speech` (Vosk) | $0 | low | No external call, but lower accuracy on elderly speech + more infra. Not recommended for v1. |

Per-call overhead: **one short utterance per window** (≤ ~3s of audio), so cost and
latency are negligible. Whisper handles elderly speech and mild accents well; keyword
matching is done **after** transcription on a strict allowlist, so STT only has to get
the key words roughly right.

---

## 6. Unit test plan (no calls)

**`test/speech-match.test.js` — `matchWellnessSpeech` truth table:**

| Transcript | Expect |
|---|---|
| "I'm okay" / "I am okay" / "I'm fine" / "I'm alright" / "all good" | `'1'` |
| "I need Darcee" / "I need my daughter" / "call Darcee" | `'2'` |
| "okay" (bare) / "yeah" / "uh huh" | `null` (too weak — rule 4) |
| "I'm not okay" / "I'm okay but I need Darcee" | `null` (ambiguous/negated — rule 3) |
| "" / "..." / background noise transcript | `null` |
| Case/punctuation/whitespace variants | normalized correctly |

**`matchYesNo`** (trash): "yes/yep/put it out" → yesDigit; "no/nope/skip it" → noDigit;
anything else → `null`.

**`test/confirm.test.js` additions (mock endpoint + injected `listenForSpeech`):**
- DTMF '1' arrives → resolves `1`, speech fn never consulted (**DTMF wins**).
- No DTMF, speech yields "I'm okay" → resolves `1`.
- No DTMF, speech yields `null` (ambiguous) → window expires → `{confirmed:false}`.
- DTMF and speech in the same window → DTMF result, speech ignored.
- Flag off / no speech fn → behaviour identical to today (regression guard).

**Guardian:** existing 45/45 suite must stay green (no outcome-model change); add a
`call_provider` payload test asserting `voiceFallback` + wording are sent only when the
flag is on, and that the resolved digit is read back unchanged.

---

## 7. Live test plan — Darcee's extension FIRST, Mom never first

1. **Unit + component** green (above); feed a few recorded WAVs ("I'm okay", "I need
   Darcee", mumble, silence) → `transcribe` → `matchWellnessSpeech`.
2. **Supervised calls to Darcee's ext 39510 only** (`CALL_PROVIDER` test path /
   `POST /guardian/test/call`), exercising every answer mode:
   - press 1 / press 2 (DTMF regression — must behave as today);
   - **say** "I'm okay" / "I need Darcee" on the **phone earpiece**;
   - same **on speaker**;
   - same **over Bluetooth / a BT speaker** (the Echo-like path);
   - say nothing / mumble → confirm it falls through to no-confirmation;
   - press a digit **while** speaking → confirm DTMF wins.
3. **Inbound:** Darcee dials ext 39515 and repeats the matrix.
4. **Trash rider:** Monday-noon path to 39510 with a spoken "yes"/"no".
5. Only after all pass: enable `GUARDIAN_VOICE_FALLBACK_ENABLED=true` and do **one
   supervised** real check with Mom present, keypad still working, Telegram backstop
   ready. Keep the Alexa-first grace + phone ladder fully intact throughout.

**No live Mom calls, no deploy, until Darcee explicitly approves each step.**

---

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **STT mishears → false "okay"** (the dangerous one) | Strict phrase allowlist (rule 4, no bare "okay"); ambiguity/negation → `null` → no confirm; `GUARDIAN_VOICE_MIN_CONFIDENCE`; **DTMF stays authoritative**; the next scheduled check re-verifies; Telegram backstop. |
| Angel's own TTS transcribed (echo/barge-in) | Capture **only after** prompt playback, within the listen window; never fork during TTS. |
| Background TV / a second person says "okay" | Require a clear first-person phrase; short capture window; if it produces a wrong "okay", next check re-verifies and Darcee sees the Telegram trail. |
| Latency/cost | One ≤3s utterance per window via Groq (~free, sub-second). |
| Elderly speech / accent | Whisper is robust; keyword list includes natural variants ("I'm alright"); DTMF + Telegram remain. |
| Regression into Max/Judy or other flows | Hard feature-gate (`voiceFallback` per-call flag); default off; confirm.js unchanged when no speech fn injected; regression test. |
| Trash false-positive | Trash is non-critical; unrecognized → unknown (as today); never affects the wellness `final_status`. |

---

## 9. Build order (when Darcee says go — NOT now)

1. `lib/speech-match.js` + unit tests (pure, zero risk).
2. `confirm.js` speech hook + tests (still no real audio).
3. `outbound-routes.js` audio-fork wiring for the wellness leg (behind flag).
4. `inbound-wellness.js` parity + trash follow-up.
5. Guardian `call_provider`/`config`/wording + `.env.example`; Guardian suite green.
6. Supervised ext-39510 matrix (§7) → then one supervised Mom check.

---

## 10. Deployment, enablement & rollback (as built)

### Deploy (behind flag — already done 2026-06-23, flags OFF)
1. **voice-app** (new lib files → image rebuild):
   `cd ~/.claude-phone && docker compose build voice-app && docker compose up -d voice-app`
   (recreates only voice-app; Max/Judy/Angel re-register in a few seconds; nothing
   else touched). New code is inert unless a call carries `voiceFallback` (outbound)
   or `VOICE_FALLBACK_ENABLED=true` is set (inbound).
2. **Guardian** (Python-only): `pm2 restart guardian-assistant --update-env`.
3. Confirm health: voice-app registrations up; Guardian `/guardian/health` OK; a
   normal DTMF check still behaves exactly as before.

### Enablement (DO NOT do until Darcee signs off after ext-39510 testing)
- **STT key** (voice-app `~/.claude-phone/.env`): `STT_API_KEY=<groq key>`,
  `STT_BASE_URL=https://api.groq.com/openai/v1`, `STT_MODEL=whisper-large-v3`.
- **Outbound** (Guardian `~/projects/guardian/.env`): `GUARDIAN_VOICE_FALLBACK_ENABLED=true`
  → `pm2 restart guardian-assistant --update-env`.
- **Inbound** (voice-app `~/.claude-phone/.env`): `VOICE_FALLBACK_ENABLED=true`
  → `docker compose up -d voice-app`.
- Test on **ext 39510** first (full matrix §7), THEN one supervised Mom check.

### Rollback
- **Instant (no redeploy):** set `GUARDIAN_VOICE_FALLBACK_ENABLED=false` (+ restart
  guardian) and/or `VOICE_FALLBACK_ENABLED=false` (+ `docker compose up -d voice-app`).
  Behavior returns to exact pre-ANGEL-13 DTMF-only — the flag is the kill switch.
- **Full revert:** `git revert` the voice-app + guardian commits, rebuild voice-app,
  restart guardian. (Not needed for a behavioral rollback — the flag suffices.)

## 11. Production rollout sequence (recommended)

1. ✅ Build + unit tests + deploy behind flag (done; flags OFF).
2. Add the Groq STT key to the voice-app env (no behavior change yet).
3. Enable **inbound** on the voice-app; call **ext 39515 from 39510** and run the
   answer-mode matrix (handset / speaker / Bluetooth / DTMF-while-speaking).
4. Enable **outbound** in Guardian; use `POST /guardian/test/call {to:"39510"}` and
   repeat the matrix; verify DTMF still wins and ambiguous speech falls through.
5. Watch one or two of **Darcee's own** scheduled-style test checks end-to-end.
6. **Only then**, with Darcee present and the Telegram backstop ready, allow it on
   one of Mom's real checks. Keep Alexa-first + the phone ladder fully intact.
7. If anything feels off at any step → flip the flag OFF (instant rollback).

_DTMF remains primary; Alexa-first, the 15-minute grace window, Telegram controls,
and Max/Judy are untouched. Built + deployed behind a default-OFF flag; not enabled
for Mom — awaiting Darcee's sign-off._
