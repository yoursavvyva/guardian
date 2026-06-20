# Angel — Alexa skill skeleton (ANGEL-09)

Lets Mom answer her wellness check **by voice at home**:
- "Alexa, tell **Angel** I'm okay" → Guardian marks her okay + cancels the pending phone check
- "Alexa, tell **Angel** I need Darcee" → Guardian opens a call-back request

Guardian stays the system of record; the phone workflow is the fallback. **This skill has
no Drop In / camera / video permission and never invokes any.**

## Files
- `interaction-model.json` — invocation name `angel`, `OkayIntent` / `NeedDarceeIntent`.
- `lambda/index.js` — intent handlers → `POST /guardian/alexa/wellness`.

## Deploy (Alexa-hosted — free, recommended)
1. developer.amazon.com → Alexa → **Create Skill** → name "Angel", **Custom** model,
   **Alexa-hosted (Node.js)**.
2. **Build → JSON Editor**: paste `interaction-model.json`. Save + Build Model.
3. **Code**: replace `index.js` with `lambda/index.js`. In the hosted code's environment
   (or inline consts), set:
   - `GUARDIAN_ALEXA_URL` = `https://<guardian-host>/guardian/alexa/wellness`
   - `GUARDIAN_ALEXA_TOKEN` = the same value as Guardian's `GUARDIAN_ALEXA_TOKEN`
   Deploy.
4. **Test** tab (or a device): "tell Angel I'm okay" → expect Guardian Telegram
   "💚 Alexa Check-In…". Test against **Darcee's** account/device first.

## Get it onto Mom's Echo devices — PLAN: Mom's own Amazon account
Mom's Echos stay on **her own Amazon account** (decided 2026-06-19), so:
- Use **Skill Beta Testing** (free, no public certification): Distribution → Beta Test →
  invite Mom's Amazon email → she taps the link to enable "Angel". No account linking
  needed (single fixed Mom + Guardian).
- Bonus: her own account gives her a distinct Alexa **contact**, so Darcee's **manual**
  Drop In / video (Echo Show 11) / chat works cleanly — set up separately in the Alexa
  app (add contact + Mom enables Drop In permission). Angel never touches that.

## Endpoint
`POST /guardian/alexa/wellness`  · header `X-Guardian-Alexa-Token: <token>`
Body: `{"intent": "okay" | "needs_darcee"}` → `{ok, outcome, checkin_id, reconciled, source:"alexa"}`
