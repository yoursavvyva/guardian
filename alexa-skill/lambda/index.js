'use strict';

const GUARDIAN_URL = process.env.GUARDIAN_ALEXA_URL || 'https://angel.darceesellers.com/guardian/alexa/wellness';
const GUARDIAN_TOKEN = process.env.GUARDIAN_ALEXA_TOKEN || 'c377cb2518f5bc6e6b63d073cf45faccf7a740d58a3e7654';

const LAUNCH_LINE = [
  "Hi Mom, it's Angel.",
  "If you're okay, say: I'm okay.",
  "Or, if you'd like Darcee to call you, say: I need Darcee."
].join(' ');

const OKAY_LINE = [
  "Wonderful.",
  "I've let Darcee know you're doing okay.",
  "Have a lovely day, Mom."
].join(' ');

const NEEDS_LINE = [
  "Okay, I've let Darcee know you'd like a call.",
  "She'll reach out soon."
].join(' ');

const HELP_LINE = "Say: I'm okay. Or say: I need Darcee.";

const BYE_LINE = "Okay, Mom. Take care.";

const ERROR_LINE = [
  "I am sorry,",
  "I could not reach Darcee's system just now.",
  "Please try again,",
  "or wait for Angel's phone call."
].join(' ');

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
    return speak(LAUNCH_LINE, handlerInput);
  }

  if (name === 'OkayIntent') {
    const r = await reportToGuardian('okay');
    return speak(r.ok ? OKAY_LINE : ERROR_LINE, handlerInput);
  }

  if (name === 'NeedDarceeIntent') {
    const r = await reportToGuardian('needs_darcee');
    return speak(r.ok ? NEEDS_LINE : ERROR_LINE, handlerInput);
  }

  if (name === 'AMAZON.HelpIntent') {
    return speak(HELP_LINE, handlerInput);
  }

  return speak(BYE_LINE, handlerInput);
};

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
