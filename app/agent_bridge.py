"""Phase 2: bridge the live receptionist to the ElevenLabs voice agent.

Twilio <Connect><Stream> websocket  <-->  ElevenLabs Agents conversation websocket

The agent must be configured for ulaw_8000 input and output (scripts/setup_agent.py
does this), so audio passes straight through without resampling.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import websockets
from fastapi import WebSocket, WebSocketDisconnect

from . import store, telephony
from .config import settings
from .models import Booking, CallSession, CallStatus

log = logging.getLogger(__name__)

EL_API = "https://api.elevenlabs.io/v1/convai"


async def _signed_url() -> str:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(
            f"{EL_API}/conversation/get-signed-url",
            params={"agent_id": settings.elevenlabs_agent_id},
            headers={"xi-api-key": settings.elevenlabs_api_key},
        )
        r.raise_for_status()
        return r.json()["signed_url"]


async def handle_agent_stream(twilio_ws: WebSocket, session_id: str) -> None:
    await twilio_ws.accept()
    session = store.get(session_id)
    if session is None:
        await twilio_ws.close()
        return

    stream_sid: str | None = None
    handing_off = False

    async with websockets.connect(await _signed_url()) as el:
        await el.send(json.dumps({
            "type": "conversation_initiation_client_data",
            "dynamic_variables": session.patient.dynamic_variables(),
        }))

        async def twilio_to_agent() -> None:
            nonlocal stream_sid
            try:
                while True:
                    msg = json.loads(await twilio_ws.receive_text())
                    event = msg.get("event")
                    if event == "start":
                        stream_sid = msg["start"]["streamSid"]
                    elif event == "media":
                        await el.send(json.dumps({"user_audio_chunk": msg["media"]["payload"]}))
                    elif event == "stop":
                        return
            except WebSocketDisconnect:
                return

        async def send_to_twilio(payload: dict) -> None:
            if stream_sid:
                await twilio_ws.send_text(json.dumps({"streamSid": stream_sid, **payload}))

        async def agent_to_twilio() -> None:
            nonlocal handing_off
            async for raw in el:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "audio":
                    await send_to_twilio({"event": "media", "media": {"payload": msg["audio_event"]["audio_base_64"]}})
                elif kind == "interruption":
                    # Receptionist spoke over the agent: flush queued audio.
                    await send_to_twilio({"event": "clear"})
                elif kind == "ping":
                    await el.send(json.dumps({"type": "pong", "event_id": msg["ping_event"]["event_id"]}))
                elif kind == "user_transcript":
                    session.log("receptionist", msg["user_transcription_event"]["user_transcript"])
                elif kind == "agent_response":
                    session.log("agent", msg["agent_response_event"]["agent_response"])
                elif kind == "client_tool_call":
                    call = msg["client_tool_call"]
                    result, is_error = await run_tool(session, call["tool_name"], call.get("parameters") or {})
                    if call["tool_name"] == "connect_patient" and not is_error:
                        handing_off = True
                    await el.send(json.dumps({
                        "type": "client_tool_result",
                        "tool_call_id": call["tool_call_id"],
                        "result": result,
                        "is_error": is_error,
                    }))
                    if handing_off:
                        # Let the agent finish saying "connecting you now", then switch.
                        asyncio.create_task(_handoff_after(session, delay=5))

        tasks = [asyncio.create_task(twilio_to_agent()), asyncio.create_task(agent_to_twilio())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()

    # Conversation finished (agent used end_call, or the receptionist hung up).
    if not handing_off and session.status == CallStatus.TALKING:
        session.status = CallStatus.ENDED
        session.outcome = session.outcome or "Call ended without a confirmed booking."
    if not handing_off and session.call_sid:
        await telephony.hang_up(session.call_sid)
    if not handing_off and not session.booking:
        await telephony.send_sms(
            session.patient.patient_phone,
            f"Your call to {session.patient.surgery_name} ended without a booking. {session.outcome}",
        )


async def run_tool(session: CallSession, name: str, params: dict) -> tuple[str, bool]:
    """Client tools the ElevenLabs agent can call (defined in scripts/setup_agent.py)."""
    if name == "record_booking":
        session.booking = Booking(
            date=str(params.get("date", "")),
            time=str(params.get("time", "")),
            clinician=str(params.get("clinician", "")),
            location=str(params.get("location", "")),
            notes=str(params.get("notes", "")),
        )
        session.status = CallStatus.BOOKED
        session.outcome = "Appointment booked."
        b = session.booking
        session.log("booking", f"{b.date} {b.time} {b.clinician} {b.location}".strip())
        sms = f"Booked at {session.patient.surgery_name}: {b.date} at {b.time}"
        if b.clinician:
            sms += f" with {b.clinician}"
        if b.location:
            sms += f" ({b.location})"
        if b.notes:
            sms += f". Note: {b.notes}"
        await telephony.send_sms(session.patient.patient_phone, sms)
        return "Booking saved and the patient has been sent a text confirmation.", False

    if name == "report_outcome":
        session.outcome = str(params.get("summary", ""))
        session.log("outcome", session.outcome)
        return "Noted.", False

    if name == "connect_patient":
        if not session.patient.allow_patient_callback:
            return "The patient did not agree to be called. Ask if they can call the patient back instead.", True
        session.log("system", "Receptionist asked to speak to the patient. Calling them now.")
        return "Calling the patient now. Tell the receptionist you are connecting them and it may take a moment.", False

    return f"Unknown tool {name}", True


async def _handoff_after(session: CallSession, delay: float) -> None:
    await asyncio.sleep(delay)
    if not session.call_sid:
        return
    session.status = CallStatus.HANDED_TO_PATIENT
    session.outcome = "Patient connected directly to the receptionist."
    await telephony.handoff_to_patient(
        session.id, session.call_sid, session.patient.patient_phone, session.patient.surgery_name
    )
