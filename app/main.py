"""FastAPI app: intake form, status API, Twilio webhooks and agent audio."""

from __future__ import annotations

import asyncio
from collections import deque
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from time import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import call_flow, store, telephony, voice
from .config import settings
from .models import CallSession, CallStatus, PatientRequest

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
_call_starts: deque[float] = deque()


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
        log.warning("Missing settings (calls will fail): %s", ", ".join(missing))
    log.info(
        "Privacy startup: SHOW_TRANSCRIPTS=%s; GEMINI_API_KEY=%s; external services=Twilio (telephony/STT/SMS), "
        "ElevenLabs (agent/TTS)%s",
        "on" if settings.show_transcripts else "off",
        "set" if os.environ.get("GEMINI_API_KEY") else "unset",
        ", Gemini (hold classifier)" if os.environ.get("GEMINI_API_KEY") else "",
    )
    if settings.show_transcripts:
        log.warning("SHOW_TRANSCRIPTS enabled: fake data only")
    cleanup_task = asyncio.create_task(store.cleanup_loop(60))
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


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


def _session(session_id: str) -> CallSession:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown call")
    return session


def _require_api_key(request: Request) -> None:
    configured = settings.app_api_key
    supplied = request.headers.get("X-API-Key", "")
    if not configured or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _check_call_rate_limit() -> None:
    now = time()
    while _call_starts and now - _call_starts[0] >= 3600:
        _call_starts.popleft()
    if settings.max_calls_per_hour > 0 and len(_call_starts) >= settings.max_calls_per_hour:
        raise HTTPException(status_code=429, detail="Call limit reached; try again later")
    _call_starts.append(now)


async def _require_twilio_signature(request: Request, form=None) -> None:
    if not settings.twilio_validate_signatures:
        return
    from twilio.request_validator import RequestValidator

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    if form is None:
        form = await request.form()
    url = f"{settings.public_url}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    if not RequestValidator(settings.twilio_auth_token).validate(url, dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ---------- Patient-facing API ----------

@app.post("/api/calls")
async def start_call(patient: PatientRequest, request: Request) -> dict:
    _require_api_key(request)
    if EMERGENCY.search(patient.reason):
        raise HTTPException(
            status_code=422,
            detail="This sounds like it could be an emergency. Call 999 now, or 111 for urgent advice. "
            "We won't wait in a GP queue for this.",
        )
    _check_call_rate_limit()
    session = CallSession(patient=patient)
    store.add(session)
    session.log("system", f"Dialling {patient.surgery_name}")
    try:
        session.call_sid = await telephony.place_call(session.id, patient.surgery_phone)
    except Exception as exc:
        session.status = CallStatus.FAILED
        session.phase = "done"
        session.outcome = f"Could not place the call: {exc}"
        raise HTTPException(status_code=502, detail=session.outcome) from exc
    return {"id": session.id}


@app.get("/api/calls/{session_id}")
async def call_status(session_id: str, request: Request) -> dict:
    _require_api_key(request)
    return _session(session_id).public_view()


# Manual controls. Trial accounts may refuse to redirect a live call; if so the
# action is queued and happens on the next phrase Twilio hears.

@app.post("/api/calls/{session_id}/takeover")
async def manual_takeover(session_id: str, request: Request) -> dict:
    _require_api_key(request)
    session = _session(session_id)
    if session.phase not in {"dialling", "hold"}:
        return {"ok": True}
    session.takeover_requested = True
    try:
        await telephony.redirect_call(session.call_sid, f"/twiml/takeover/{session_id}")
    except Exception as exc:
        log.info("Redirect refused (%s); taking over on the next phrase", exc)
        session.log("system", "Agent will take over as soon as the receptionist speaks.")
    return {"ok": True}


@app.post("/api/calls/{session_id}/digits/{digits}")
async def manual_digits(session_id: str, digits: str, request: Request) -> dict:
    _require_api_key(request)
    if not re.fullmatch(r"[0-9*#]{1,6}", digits):
        raise HTTPException(status_code=400, detail="Digits must be 0-9, * or #")
    session = _session(session_id)
    if session.phase != "hold":
        raise HTTPException(status_code=409, detail="Keys can only be pressed while on hold")
    try:
        await telephony.redirect_call(session.call_sid, f"/twiml/digits/{session_id}/{digits}")
    except Exception as exc:
        log.info("Redirect refused (%s); pressing keys on the next phrase", exc)
        session.pending_digits = digits
        session.log("system", f"Will press {digits} after the next announcement.")
    return {"ok": True}


@app.post("/api/calls/{session_id}/cancel")
async def cancel(session_id: str, request: Request) -> dict:
    _require_api_key(request)
    session = _session(session_id)
    was_live = session.phase != "done"
    session.phase = "done"  # the next callback hangs up, even if the REST hang-up is refused
    session.status = CallStatus.ENDED
    session.outcome = "Cancelled by you."
    session.log("system", session.outcome)
    if was_live and session.call_sid:
        await telephony.hang_up(session.call_sid)
    return {"ok": True}


# ---------- Twilio webhooks ----------

@app.post("/twiml/start/{session_id}")
async def twiml_start(session_id: str, request: Request) -> Response:
    await _require_twilio_signature(request)
    session = store.get(session_id)
    if session is None or session.phase == "done":
        return _twiml(telephony.twiml_hangup())
    return _twiml(call_flow.call_answered(session))


@app.post("/twiml/hold/{session_id}")
async def twiml_hold(session_id: str, request: Request) -> Response:
    """Reached only after ~10 minutes of silence on hold."""
    await _require_twilio_signature(request)
    session = store.get(session_id)
    if session is None or session.phase == "done":
        return _twiml(telephony.twiml_hangup())
    session.hops += 1
    return _twiml(telephony.twiml_hold(session_id))


@app.post("/twiml/heard/{session_id}")
async def twiml_heard(session_id: str, request: Request) -> Response:
    session = store.get(session_id)
    if session is None:
        return _twiml(telephony.twiml_hangup())
    form = await request.form()
    await _require_twilio_signature(request, form)
    text = str(form.get("SpeechResult", ""))
    return _twiml(await call_flow.heard(session, text))


@app.post("/twiml/takeover/{session_id}")
async def twiml_takeover(session_id: str, request: Request) -> Response:
    await _require_twilio_signature(request)
    session = store.get(session_id)
    if session is None or session.phase == "done":
        return _twiml(telephony.twiml_hangup())
    if session.phase == "agent":
        return _twiml(telephony.twiml_reply(session_id, None))
    return _twiml(call_flow.take_over(session, "", "manual override"))


@app.post("/twiml/digits/{session_id}/{digits}")
async def twiml_digits(session_id: str, digits: str, request: Request) -> Response:
    await _require_twilio_signature(request)
    session = store.get(session_id)
    if session is None or session.phase == "done":
        return _twiml(telephony.twiml_hangup())
    return _twiml(call_flow.press_digits(session, re.sub(r"[^0-9*#]", "", digits)))


@app.post("/twiml/patient/{session_id}")
async def twiml_patient(session_id: str, request: Request) -> Response:
    await _require_twilio_signature(request)
    session = _session(session_id)
    return _twiml(telephony.twiml_conference(
        session_id,
        starts_conference=True,
        intro=f"Hello, this is your GP booking assistant. The receptionist at {session.patient.surgery_name} "
        "would like to speak with you. Connecting you now.",
    ))


@app.post("/twilio/status/{session_id}")
async def twilio_status(session_id: str, request: Request) -> Response:
    form = await request.form()
    await _require_twilio_signature(request, form)
    session = store.get(session_id)
    status = str(form.get("CallStatus", ""))
    if session:
        session.log("twilio", f"Call {status}")
        if status in {"busy", "no-answer", "failed", "canceled"}:
            call_flow.give_up(session, f"The surgery line was {status.replace('-', ' ')}. Please try again later.")
        elif status == "completed" and session.status != CallStatus.HANDED_TO_PATIENT:
            call_flow.finish(session)
    return Response(status_code=204)


# ---------- Agent audio for <Play> ----------

@app.get("/audio/{clip_id}.mp3")
async def audio(clip_id: str) -> Response:
    clip = voice.get(clip_id)
    if clip is None or not await clip.wait_for_audio():
        raise HTTPException(status_code=404, detail="No audio")
    return StreamingResponse(clip.stream(), media_type="audio/mpeg")

