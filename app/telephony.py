"""Twilio: placing the call, steering it between phases, and SMS.

Call phases, each driven by swapping the call's TwiML:

1. hold    - <Start><Stream> forks GP audio to /ws/hold/{id} (listen only) while
             <Pause> keeps the line open. Our server transcribes and classifies it.
2. dtmf    - <Play digits> answers IVR menus; the forked stream keeps running.
3. agent   - <Stop> the fork, <Connect><Stream> to /ws/agent/{id}: a two-way
             audio bridge to the ElevenLabs voice agent.
4. handoff - optional: GP and patient are joined in a Twilio conference.
"""

from __future__ import annotations

import asyncio
import logging

from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Dial, Start, Stop, VoiceResponse

from .config import settings

log = logging.getLogger(__name__)

HOLD_STREAM = "hold-listener"
PAUSE_SECONDS = 600


def _client() -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


# ---------- TwiML builders (pure functions, unit-tested) ----------

def twiml_start_hold(session_id: str) -> str:
    r = VoiceResponse()
    start = Start()
    start.stream(name=HOLD_STREAM, url=f"{settings.public_ws_url}/ws/hold/{session_id}", track="inbound_track")
    r.append(start)
    r.pause(length=PAUSE_SECONDS)
    r.redirect(f"{settings.public_url}/twiml/keep-holding/{session_id}", method="POST")
    return str(r)


def twiml_keep_holding(session_id: str) -> str:
    r = VoiceResponse()
    r.pause(length=PAUSE_SECONDS)
    r.redirect(f"{settings.public_url}/twiml/keep-holding/{session_id}", method="POST")
    return str(r)


def twiml_press_digits(session_id: str, digits: str) -> str:
    r = VoiceResponse()
    # 'w' = half-second wait, so the IVR has finished speaking.
    r.play(digits="w" + digits)
    r.pause(length=PAUSE_SECONDS)
    r.redirect(f"{settings.public_url}/twiml/keep-holding/{session_id}", method="POST")
    return str(r)


def twiml_connect_agent(session_id: str) -> str:
    r = VoiceResponse()
    stop = Stop()
    stop.stream(name=HOLD_STREAM)
    r.append(stop)
    connect = Connect()
    stream = connect.stream(url=f"{settings.public_ws_url}/ws/agent/{session_id}")
    stream.parameter(name="session_id", value=session_id)
    r.append(connect)
    return str(r)


def twiml_conference(room: str, *, starts_conference: bool, intro: str = "") -> str:
    r = VoiceResponse()
    if intro:
        r.say(intro, voice="Polly.Amy", language="en-GB")
    dial = Dial()
    dial.conference(
        room,
        start_conference_on_enter=starts_conference,
        end_conference_on_exit=True,
        beep=False,
    )
    r.append(dial)
    return str(r)


# ---------- Actions on a live call ----------

async def place_call(session_id: str, to_number: str) -> str:
    def _create() -> str:
        call = _client().calls.create(
            to=to_number,
            from_=settings.twilio_phone_number,
            url=f"{settings.public_url}/twiml/start/{session_id}",
            status_callback=f"{settings.public_url}/twilio/status/{session_id}",
            status_callback_event=["answered", "completed"],
            time_limit=4 * 60 * 60,
        )
        return call.sid

    return await asyncio.to_thread(_create)


async def update_call(call_sid: str, twiml: str) -> None:
    await asyncio.to_thread(lambda: _client().calls(call_sid).update(twiml=twiml))


async def hang_up(call_sid: str) -> None:
    try:
        await asyncio.to_thread(lambda: _client().calls(call_sid).update(status="completed"))
    except Exception:  # call may already be over
        log.info("hang_up: call %s already finished", call_sid)


async def handoff_to_patient(session_id: str, gp_call_sid: str, patient_phone: str, surgery: str) -> None:
    """Ring the patient and join them with the receptionist in a conference.

    The receptionist hears Twilio's hold music until the patient picks up.
    """
    room = f"gp-{session_id}"
    patient_twiml = twiml_conference(
        room,
        starts_conference=True,
        intro=f"Hello, this is your GP booking assistant. The receptionist at {surgery} "
        "would like to speak with you. Connecting you now.",
    )

    def _dial_patient() -> None:
        _client().calls.create(to=patient_phone, from_=settings.twilio_phone_number, twiml=patient_twiml)

    await asyncio.to_thread(_dial_patient)
    await update_call(gp_call_sid, twiml_conference(room, starts_conference=False))


async def send_sms(to: str, body: str) -> None:
    if not settings.twilio_phone_number:
        return
    try:
        await asyncio.to_thread(
            lambda: _client().messages.create(to=to, from_=settings.twilio_phone_number, body=body)
        )
    except Exception as exc:
        log.warning("SMS to patient failed: %s", exc)
