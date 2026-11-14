"""FastAPI app: intake form, status API, Twilio webhooks and media websockets."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response

from . import store, telephony
from .agent_bridge import handle_agent_stream
from .config import settings
from .hold_listener import handle_hold_stream, press_digits, take_over
from .models import TERMINAL, CallSession, CallStatus, PatientRequest

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    missing = [
        name
        for name, value in {
            "PUBLIC_URL": settings.public_url,
            "TWILIO_ACCOUNT_SID": settings.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": settings.twilio_auth_token,
            "TWILIO_PHONE_NUMBER": settings.twilio_phone_number,
            "ELEVENLABS_API_KEY": settings.elevenlabs_api_key,
            "ELEVENLABS_AGENT_ID": settings.elevenlabs_agent_id,
        }.items()
        if not value
    ]
    if missing:
        logging.warning("Missing settings (calls will fail): %s", ", ".join(missing))
    yield


app = FastAPI(title="GP Queue Assistant", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"

# If the reason sounds like an emergency, don't queue: send them to 999 / 111.
EMERGENCY = re.compile(
    r"chest pain|can'?t breathe|cannot breathe|struggling to breathe|stroke|face drooping|"
    r"unconscious|seizure|fitting|severe bleeding|suicid|overdose|anaphyla",
    re.I,
)


def _twiml(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/calls")
async def start_call(patient: PatientRequest) -> dict:
    if EMERGENCY.search(patient.reason):
        raise HTTPException(
            status_code=422,
            detail="This sounds like it could be an emergency. Call 999 now, or 111 for urgent advice. "
            "We won't wait in a GP queue for this.",
        )
    session = CallSession(patient=patient)
    store.add(session)
    session.log("system", f"Dialling {patient.surgery_name}")
    try:
        session.call_sid = await telephony.place_call(session.id, patient.surgery_phone)
    except Exception as exc:
        session.status = CallStatus.FAILED
        session.outcome = f"Could not place the call: {exc}"
        raise HTTPException(status_code=502, detail=session.outcome) from exc
    return {"id": session.id}


@app.get("/api/calls/{session_id}")
async def call_status(session_id: str) -> dict:
    session = _session(session_id)
    return session.public_view()


@app.post("/api/calls/{session_id}/takeover")
async def manual_takeover(session_id: str) -> dict:
    """Demo/backup control: 'a human has answered, connect the agent now'."""
    await take_over(_session(session_id), "manual override")
    return {"ok": True}


@app.post("/api/calls/{session_id}/digits/{digits}")
async def manual_digits(session_id: str, digits: str) -> dict:
    if not re.fullmatch(r"[0-9*#]{1,6}", digits):
        raise HTTPException(status_code=400, detail="Digits must be 0-9, * or #")
    await press_digits(_session(session_id), digits)
    return {"ok": True}


@app.post("/api/calls/{session_id}/cancel")
async def cancel(session_id: str) -> dict:
    session = _session(session_id)
    if session.call_sid and session.status not in TERMINAL:
        await telephony.hang_up(session.call_sid)
    session.status = CallStatus.ENDED
    session.outcome = "Cancelled by you."
    session.log("system", session.outcome)
    return {"ok": True}


# ---------- Twilio webhooks ----------
# TODO before real use: validate X-Twilio-Signature (twilio.request_validator).

@app.post("/twiml/start/{session_id}")
async def twiml_start(session_id: str) -> Response:
    return _twiml(telephony.twiml_start_hold(session_id))


@app.post("/twiml/keep-holding/{session_id}")
async def twiml_keep_holding(session_id: str) -> Response:
    return _twiml(telephony.twiml_keep_holding(session_id))


@app.post("/twilio/status/{session_id}")
async def twilio_status(session_id: str, request: Request) -> Response:
    form = await request.form()
    session = store.get(session_id)
    status = str(form.get("CallStatus", ""))
    if session:
        session.log("twilio", f"Call {status}")
        if status in {"busy", "no-answer", "failed", "canceled"}:
            session.status = CallStatus.FAILED
            session.outcome = f"The surgery line was {status.replace('-', ' ')}."
            await telephony.send_sms(session.patient.patient_phone, session.outcome + " Please try again later.")
        elif status == "completed" and session.status not in TERMINAL:
            session.status = CallStatus.ENDED
            session.outcome = session.outcome or "The call ended before a booking was made."
    return Response(status_code=204)


# ---------- Media websockets ----------

@app.websocket("/ws/hold/{session_id}")
async def ws_hold(ws: WebSocket, session_id: str) -> None:
    await handle_hold_stream(ws, session_id)


@app.websocket("/ws/agent/{session_id}")
async def ws_agent(ws: WebSocket, session_id: str) -> None:
    await handle_agent_stream(ws, session_id)


def _session(session_id: str) -> CallSession:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown call")
    return session
