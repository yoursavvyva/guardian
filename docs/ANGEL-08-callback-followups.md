# ANGEL-08 add-on — Monday trash question recovered on a call-back

_Built 2026-06-19. Extends the inbound call-back (Mom calls Angel ext 39515) so that if
she **missed the Monday 12 PM check** and calls back, Angel asks the **same trash
question** that rode that missed call — preserving both parts (wellness + trash)._

## Flow (Monday-noon missed → call-back)

```
Angel: "Hi Mom, it's Angel. I'm glad you called back.
        Are you okay today? Press 1 for yes. Press 2 if you need Darcee to call you."
   → wellness DTMF (press 1 / 2 / none)  → wellness ack
Angel: "Do you need the trash taken out? Press 1 for yes. Press 2 for no."
   → trash DTMF (press 1 / 2 / none)      → trash ack
```

The trash question is asked **after** the wellness response and **regardless** of the
wellness answer. The two are tracked **separately** — a trash answer never changes the
wellness status.

## Outcomes

**Wellness** (recorded as the call-back outcome / `last_callback_outcome`):
| Press | Outcome | Effect |
|---|---|---|
| 1 | `callback_confirmed_ok` | satisfies + cancels today's pending phone check (ANGEL-08 reconcile) |
| 2 | `callback_needs_darcee` | opens a needs_darcee request (ack/reminder loop) |
| none | `callback_no_response` | satisfies nothing; 🔔 notify |

**Trash** (recorded on the missed Monday-noon check's `trash_result`, separate column):
| Press | Outcome | Effect |
|---|---|---|
| 1 | `trash_needed` | `trash_result='yes'` + 🗑️ Telegram (same alert + sister "Got it" button as the original Monday flow) |
| 2 | `trash_not_needed` | `trash_result='no'` + Telegram (per ANGEL-07.1, no also notifies) |
| none | `trash_unknown` | `trash_result='unknown'`; logged only — **does not block the wellness outcome** |

## Strict gating (when the trash follow-up is asked)

Only when there is a **today, Monday, 12:00 PM, trash-rider** check with **no trash
answer yet** (`_pending_trash_callback`). Because `_is_trash_check` requires
`day == trash_day` AND `time == trash_time`:
- **Non-Monday call-backs → no trash prompt.**
- **8 PM call-backs → no trash prompt** (the 8 PM check never carries the trash rider).
- Already-answered trash → not re-asked.

## Architecture (extensible)

Guardian is the brain; the voice-app executes. The inbound wellness POST response
carries a generic **`followups`** list:

```
POST /guardian/inbound/wellness  →  { outcome, checkin_id, reconciled,
                                       followups: [ { key:"trash", target_checkin_id,
                                                      message, reprompt, accept_digits,
                                                      yes_digit, ack } ] }
```

The voice-app asks each follow-up (same DTMF gather + ack as wellness) and reports it:

```
POST /guardian/inbound/followup  {checkin_id, key, digit}  →  record_callback_followup()
```

`record_callback_followup()` dispatches by **`key`** (today only `"trash"`). **To add a
future task/reminder question:** append an entry in `_callback_followups()` and add a
`key` branch in `record_callback_followup()`. The voice-app loop needs no changes.

## Guarantees
- Trash tracked separately from wellness (`trash_result` vs `final_status`); a trash
  answer never overrides the wellness status.
- Idempotent: a trash answer is recorded once; re-answers are no-ops.
- Unchanged: the phone dialing ladder, the 8 PM check, Max/Judy, and the Alexa plan.
