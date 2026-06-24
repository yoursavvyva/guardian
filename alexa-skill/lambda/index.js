'use strict';

const https = require('https');

const GUARDIAN_URL = process.env.GUARDIAN_ALEXA_URL || 'https://angel.darceesellers.com/guardian/alexa/wellness';
const GUARDIAN_TOKEN = process.env.GUARDIAN_ALEXA_TOKEN || 'c377cb2518f5bc6e6b63d073cf45faccf7a740d58a3e7654';

const LAUNCH_LINE = [
  "Hi Mom, it's Angel, just checking in.",
  "If you're okay, say: I'm okay.",
  "Or, if you'd like a call, say: call me."
].join(' ');

const OKAY_LINE = [
  "Thank you, Mom.",
  "I'm glad you're okay.",
  "Have a wonderful day."
].join(' ');

const NEEDS_LINE = [
  "Thank you, Mom.",
  "I'll let Darcee know you'd like a call.",
  "Talk to you later."
].join(' ');

const HELP_LINE = "Say: I'm okay. Or say: call me.";

const BYE_LINE = "Okay, Mom. Take care.";

const ERROR_LINE = [
  "I am sorry,",
  "I could not reach Darcee's system just now.",
  "Please try again,",
  "or wait for Angel's phone call."
].join(' ');

function reportToGuardian(intent) {
  return new Promise(function (resolve) {
    if (!GUARDIAN_URL || !GUARDIAN_TOKEN) {
      console.error('Guardian URL/token not configured');
      return resolve({ ok: false });
    }
    try {
      var u = new URL(GUARDIAN_URL);
      var payload = JSON.stringify({ intent: intent });
      var options = {
        hostname: u.hostname,
        port: u.port || 443,
        path: u.pathname,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload),
          'X-Guardian-Alexa-Token': GUARDIAN_TOKEN
        }
      };
      var req = https.request(options, function (res) {
        res.on('data', function () {});
        res.on('end', function () {
          resolve({ ok: res.statusCode >= 200 && res.statusCode < 300 });
        });
      });
      req.on('error', function (e) {
        console.error('Guardian POST failed:', e.message);
        resolve({ ok: false });
      });
      req.write(payload);
      req.end();
    } catch (e) {
      console.error('Guardian POST failed:', e.message);
      resolve({ ok: false });
    }
  });
}

function speak(text, handlerInput) {
  return handlerInput.responseBuilder.speak(text).withShouldEndSession(true).getResponse();
}

function intentName(handlerInput) {
  var r = handlerInput.requestEnvelope.request;
  return r.type === 'IntentRequest' ? r.intent.name : r.type;
}

exports.handler = async function (event, context) {
  var handlerInput = {
    requestEnvelope: event,
    responseBuilder: makeResponseBuilder()
  };
  var name = intentName(handlerInput);

  if (name === 'LaunchRequest') {
    return speak(LAUNCH_LINE, handlerInput);
  }

  if (name === 'OkayIntent') {
    var ok = await reportToGuardian('okay');
    return speak(ok.ok ? OKAY_LINE : ERROR_LINE, handlerInput);
  }

  if (name === 'NeedDarceeIntent') {
    var nd = await reportToGuardian('needs_darcee');
    return speak(nd.ok ? NEEDS_LINE : ERROR_LINE, handlerInput);
  }

  if (name === 'AMAZON.HelpIntent') {
    return speak(HELP_LINE, handlerInput);
  }

  return speak(BYE_LINE, handlerInput);
};

function makeResponseBuilder() {
  var response = { version: '1.0', response: { shouldEndSession: true } };
  var builder = {
    speak: function (text) {
      response.response.outputSpeech = { type: 'PlainText', text: text };
      return builder;
    },
    withShouldEndSession: function (v) {
      response.response.shouldEndSession = v;
      return builder;
    },
    getResponse: function () { return response; }
  };
  return builder;
}
