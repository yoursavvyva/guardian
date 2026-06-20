/**
 * Angel — Alexa skill skeleton (ANGEL-09).
 *
 * Handles Mom's spoken wellness response at home and relays it to Guardian (the system
 * of record). Two intents only:
 *   OkayIntent       "Alexa, tell Angel I'm okay"      -> POST {intent:"okay"}
 *   NeedDarceeIntent "Alexa, tell Angel I need Darcee" -> POST {intent:"needs_darcee"}
 *
 * Guardian reuses the ANGEL-08 reconcile, so 'okay' satisfies/cancels the pending phone
 * check and 'needs_darcee' opens a call-back request. source='alexa'.
 *
 * SECURITY: this skill has NO Drop In, camera, or video permission and never invokes
 * any. It only sends a wellness intent to one Guardian endpoint with a shared token.
 *
 * Deploy: Alexa-hosted skill (free Lambda). Set two env vars on the skill's code:
 *   GUARDIAN_ALEXA_URL   = https://<your-guardian-host>/guardian/alexa/wellness
 *   GUARDIAN_ALEXA_TOKEN = <same value as Guardian's GUARDIAN_ALEXA_TOKEN>
 *
 * No external deps (uses Node 18+ global fetch on the Alexa-hosted runtime).
 */

'use strict';

const GUARDIAN_URL = process.env.GUARDIAN_ALEXA_URL || '';
const GUARDIAN_TOKEN = process.env.GUARDIAN_ALEXA_TOKEN || '';

async function reportToGuardian(intent) {
  if (!GUARDIAN_URL || !GUARDIAN_TOKEN) {
    console.error('Guardian URL/token not configured');
    return { ok: false };
  }
  try {
    const resp = await fetch(GUARDIAN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Guardian-Alexa-Token': GUARDIAN_TOKEN },
      body: JSON.stringify({ intent })
    });
    const data = await resp.json().catch(() => ({}));
    return { ok: resp.ok, data };
  } catch (e) {
    console.error('Guardian POST failed:', e.message);
    return { ok: false };
  }
}

function speak(text, handlerInput) {
  return handlerInput.responseBuilder.speak(text).withShouldEndSession(true).getResponse();
}

function intentName(handlerInput) {
  const r = handlerInput.requestEnvelope.request;
  return r.type === 'IntentRequest' ? r.intent.name : r.type;
}

exports.handler = async function (event, context) {
  const handlerInput = {
    requestEnvelope: event,
    responseBuilder: makeResponseBuilder()
  };
  const name = intentName(handlerInput);

  if (name === 'LaunchRequest') {
    return speak("Hi Mom, it's Angel. If you're okay, say: I'm okay. " +
                 "Or, if you'd like Darcee to call you, say: I need Darcee.", handlerInput);
  }

  if (name === 'OkayIntent') {
    const r = await reportToGuardian('okay');
    return speak(r.ok
      ? "Wonderful. I've let Darcee know you're doing okay. Have a lovely day, Mom."
      : "I'm sorry, I couldn't reach Darcee's system just now. If you can, please try again, "
        + "or wait for Angel's phone call.", handlerInput);
  }

  if (name === 'NeedDarceeIntent') {
    const r = await reportToGuardian('needs_darcee');
    return speak(r.ok
      ? "Okay, I've let Darcee know you'd like a call. She'll reach out soon."
      : "I'm sorry, I couldn't reach Darcee's system just now. If you can, please try again, "
        + "or wait for Angel's phone call.", handlerInput);
  }

  if (name === 'AMAZON.HelpIntent') {
    return speak("Say: I'm okay. Or say: I need Darcee.", handlerInput);
  }

  // Stop / Cancel / anything else
  return speak("Okay, Mom. Take care.", handlerInput);
};

/**
 * Minimal response builder so this skeleton runs with or without the ask-sdk-core
 * package. If you scaffold the skill with ask-sdk, replace exports.handler with the
 * standard SkillBuilders.custom().addRequestHandlers(...) wiring — the Guardian calls
 * above stay identical.
 */
function makeResponseBuilder() {
  const response = { version: '1.0', response: { shouldEndSession: true } };
  const builder = {
    speak(text) {
      response.response.outputSpeech = { type: 'PlainText', text };
      return builder;
    },
    withShouldEndSession(v) {
      response.response.shouldEndSession = v;
      return builder;
    },
    getResponse() { return response; }
  };
  return builder;
}
