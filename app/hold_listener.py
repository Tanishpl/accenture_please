"""Phase 1: sit in the GP queue cheaply.

Twilio forks the call audio (8kHz mu-law) to this websocket. We stream it to
Deepgram for live transcription, classify each finished utterance, press IVR
keys when asked, and hand over to the ElevenLabs agent once a human answers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from . import store, telephony
from .classifier import Heard, HoldClassifier
from .config import settings
from .models import CallSession, CallStatus

log = logging.getLogger(__name__)

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000&channels=1"
    "&model=nova-3&language=en-GB&punctuate=true&smart_format=true"
    "&interim_results=false&endpointing=600&utterance_end_ms=1500"
)
DTMF_COOLDOWN_SECONDS = 8


async def take_over(session: CallSession, why: str) -> None:
    """Move the call from 'listening on hold' to 'ElevenLabs agent talks'."""
    if session.status != CallStatus.ON_HOLD or not session.call_sid:
        return
    session.status = CallStatus.TALKING
    session.answered_at = time.time()
    session.log("system", f"Receptionist answered ({why}). Connecting voice agent.")
    await telephony.update_call(session.call_sid, telephony.twiml_connect_agent(session.id))


async def press_digits(session: CallSession, digits: str) -> None:
    if session.call_sid:
        session.log("dtmf", f"Pressed {digits}")
        await telephony.update_call(session.call_sid, telephony.twiml_press_digits(session.id, digits))


async def handle_hold_stream(ws: WebSocket, session_id: str) -> None:
    await ws.accept()
    session = store.get(session_id)
    if session is None:
        await ws.close()
        return
    session.status = CallStatus.ON_HOLD
    session.log("system", "Call connected. Waiting in the queue.")

    if not settings.deepgram_api_key:
        session.log("system", "No DEEPGRAM_API_KEY: use the 'Receptionist answered' button on the status page.")
        await _drain(ws)
        return

    classifier = HoldClassifier(session.patient.surgery_name, session.patient.reason)
    last_dtmf = 0.0

    async with websockets.connect(
        DEEPGRAM_URL, additional_headers={"Authorization": f"Token {settings.deepgram_api_key}"}
    ) as dg:

        async def pump_audio() -> None:
            try:
                while True:
                    msg = json.loads(await ws.receive_text())
                    if msg.get("event") == "media":
                        await dg.send(base64.b64decode(msg["media"]["payload"]))
                    elif msg.get("event") == "stop":
                        break
            except WebSocketDisconnect:
                pass
            finally:
                try:
                    await dg.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    pass

        async def read_transcripts() -> None:
            nonlocal last_dtmf
            async for raw in dg:
                data = json.loads(raw)
                if data.get("type") != "Results" or not data.get("is_final"):
                    continue
                text = data["channel"]["alternatives"][0].get("transcript", "").strip()
                if not text or session.status != CallStatus.ON_HOLD:
                    continue
                verdict = await classifier.classify(text)
                session.log("heard", f"[{verdict.heard.value}] {text}")

                if verdict.heard == Heard.HUMAN:
                    await take_over(session, verdict.reason)
                    return
                if verdict.heard == Heard.IVR_MENU and verdict.digits:
                    if time.time() - last_dtmf > DTMF_COOLDOWN_SECONDS:
                        last_dtmf = time.time()
                        await press_digits(session, verdict.digits)
                elif verdict.heard == Heard.CLOSED:
                    session.status = CallStatus.FAILED
                    session.outcome = "The surgery is closed or its queue is full."
                    session.log("system", session.outcome)
                    await telephony.hang_up(session.call_sid)
                    await telephony.send_sms(
                        session.patient.patient_phone,
                        f"We couldn't get through to {session.patient.surgery_name}: {verdict.reason}. "
                        "Please try again during opening hours.",
                    )
                    return

        async def give_up_eventually() -> None:
            await asyncio.sleep(settings.max_hold_minutes * 60)
            if session.status == CallStatus.ON_HOLD:
                session.status = CallStatus.FAILED
                session.outcome = f"Still in the queue after {settings.max_hold_minutes} minutes, so we hung up."
                session.log("system", session.outcome)
                await telephony.hang_up(session.call_sid)
                await telephony.send_sms(session.patient.patient_phone, session.outcome)

        tasks = [asyncio.create_task(t()) for t in (pump_audio, read_transcripts, give_up_eventually)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()


async def _drain(ws: WebSocket) -> None:
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("event") == "stop":
                return
    except WebSocketDisconnect:
        return
