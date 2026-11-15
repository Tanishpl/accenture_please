"""Twilio: placing the call, building TwiML for each step, and SMS.

Built to run on a Twilio *trial* account, which blocks <Stream> (no live audio)
and ends the call after ~10 action/redirect callbacks. So the call is a loop of
<Gather input="speech">: Twilio transcribes each phrase and posts it to
/twiml/heard/{id}, and our reply is the next piece of TwiML.

- Several <Gather>s are stacked in each response, so silence and hold music
  fall through to the next one without costing a callback.
- The agent's reply plays *inside* the <Gather>, so the receptionist can talk
  over it and nothing extra is needed to start listening again.
"""

from __future__ import annotations

import asyncio
import logging

from twilio.rest import Client
from twilio.twiml.voice_response import Dial, Gather, VoiceResponse

from . import voice
from .config import settings

log = logging.getLogger(__name__)

HOLD_GATHERS = 10          # 10 x 60s of silence before we spend a callback on a redirect
HOLD_TIMEOUT_SECONDS = 60
HOLD_HINTS = "press, appointments, reception, surgery, speaking, good morning, good afternoon, how can I help"
STILL_THERE = "Sorry, are you still there?"
GIVE_UP = "Sorry, I'll have to try again later. Goodbye."
POLLY_VOICE = "Polly.Amy"
DESTINATION_ERROR = "Destination is not allowed while TEST_MODE is enabled."


def _check_destination(number: str) -> None:
    if settings.test_mode and number not in settings.allowed_destinations:
        raise ValueError(DESTINATION_ERROR)


def _client() -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def _url(path: str) -> str:
    return f"{settings.public_url}{path}"


def speak(parent: VoiceResponse | Gather, text: str) -> None:
    """ElevenLabs audio if configured, otherwise Twilio's built-in Polly voice."""
    text = voice.strip_tone_tags(text)
    if settings.elevenlabs_api_key:
        parent.play(voice.clip_url(text))
    else:
        parent.say(text, voice=POLLY_VOICE, language="en-GB")


def _gather(parent: VoiceResponse, session_id: str, timeout: int, hints: str | None = None) -> Gather:
    return parent.gather(
        input="speech",
        action=_url(f"/twiml/heard/{session_id}"),
        method="POST",
        timeout=timeout,
        speech_timeout="auto",
        speech_model="phone_call",
        enhanced=True,
        language="en-GB",
        hints=hints,
    )


# ---------- TwiML builders ----------

def twiml_hold(session_id: str, digits: str | None = None) -> str:
    """Listen to the queue. Optionally press IVR keys first."""
    r = VoiceResponse()
    if digits:
        # 'w' = half-second wait, so the IVR has finished speaking.
        r.play(digits="w" + digits)
    for _ in range(HOLD_GATHERS):
        _gather(r, session_id, HOLD_TIMEOUT_SECONDS, HOLD_HINTS)
    r.redirect(_url(f"/twiml/hold/{session_id}"), method="POST")
    return str(r)


def twiml_reply(session_id: str, text: str | None) -> str:
    """Say the agent's line, then wait for the receptionist to answer."""
    r = VoiceResponse()
    g = _gather(r, session_id, timeout=8)
    if text:
        speak(g, text)
    speak(_gather(r, session_id, timeout=12), STILL_THERE)
    speak(r, GIVE_UP)
    r.hangup()
    return str(r)


def twiml_goodbye(text: str) -> str:
    r = VoiceResponse()
    if text:
        speak(r, text)
    r.hangup()
    return str(r)


def twiml_hangup() -> str:
    r = VoiceResponse()
    r.hangup()
    return str(r)


def conference_room(session_id: str) -> str:
    return f"gp-{session_id}"


def twiml_conference(session_id: str, *, starts_conference: bool, intro: str = "") -> str:
    r = VoiceResponse()
    if intro:
        speak(r, intro)
    dial = Dial()
    dial.conference(
        conference_room(session_id),
        start_conference_on_enter=starts_conference,
        end_conference_on_exit=True,
        beep=False,
    )
    r.append(dial)
    return str(r)


# ---------- REST actions ----------

async def place_call(session_id: str, to_number: str) -> str:
    _check_destination(to_number)

    def _create() -> str:
        call = _client().calls.create(
            to=to_number,
            from_=settings.twilio_phone_number,
            url=_url(f"/twiml/start/{session_id}"),
            status_callback=_url(f"/twilio/status/{session_id}"),
            status_callback_event=["answered", "completed"],
            # No time_limit: trial accounts reject it, and Twilio's default is already 4 hours.
        )
        return call.sid

    return await asyncio.to_thread(_create)


async def redirect_call(call_sid: str, path: str) -> None:
    """Point a live call at new TwiML. May be refused on trial accounts."""
    await asyncio.to_thread(lambda: _client().calls(call_sid).update(url=_url(path), method="POST"))


async def hang_up(call_sid: str) -> None:
    try:
        await asyncio.to_thread(lambda: _client().calls(call_sid).update(status="completed"))
    except Exception as exc:  # call may already be over
        log.info("hang_up %s: %s", call_sid, exc)


async def call_patient(session_id: str, patient_phone: str) -> None:
    """Ring the patient; their TwiML (/twiml/patient/{id}) joins them to the conference."""
    _check_destination(patient_phone)
    await asyncio.to_thread(
        lambda: _client().calls.create(
            to=patient_phone, from_=settings.twilio_phone_number, url=_url(f"/twiml/patient/{session_id}")
        )
    )


async def send_sms(to: str, body: str) -> None:
    _check_destination(to)
    if not settings.twilio_phone_number:
        return
    try:
        await asyncio.to_thread(
            lambda: _client().messages.create(to=to, from_=settings.twilio_phone_number, body=body)
        )
    except Exception as exc:
        log.warning("SMS to patient failed: %s", exc)
