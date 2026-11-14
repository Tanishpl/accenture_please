"""FastAPI app. Serves the UI and API on localhost only.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import delegation, speech, telephony, tunnel
from app.call import CallRefused, run_call
from app.interview import InterviewSession, _split_list, build_spoken_summary, ollama_available, ollama_model
from app.record import Preferences, Record, append_audit, get_audit, init_db, save_record
from mock.receptionist import PRACTICE_NUMBER

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="GP booking agent (local-first)")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
init_db()


@app.on_event("startup")
def _warm_up_speech() -> None:
    if speech.STT_AVAILABLE:
        threading.Thread(target=speech.load_whisper, daemon=True).start()


@app.on_event("shutdown")
def _stop_helpers() -> None:
    tunnel.stop()
    speech.stop_tts()


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "cf-connecting-ip", "cf-ray", "ngrok-trace-id")


@app.middleware("http")
async def only_twilio_through_tunnel(request: Request, call_next):
    """Anything arriving via the public tunnel may only reach /twilio/*.

    Patient pages and APIs stay reachable from this device only.
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    via_tunnel = host not in _LOCAL_HOSTS or any(h in request.headers for h in _PROXY_HEADERS)
    if via_tunnel and not request.url.path.startswith("/twilio/"):
        return PlainTextResponse("Not found", status_code=404)
    return await call_next(request)


@dataclass
class Session:
    interview: InterviewSession
    record: Optional[Record] = None
    credential: Optional[dict] = None
    audio_bytes: int = 0
    last_call: Optional[dict] = None
    calls_spoken: list[str] = field(default_factory=list)
    live_spoken: list[str] = field(default_factory=list)
    live_call_id: Optional[str] = None


SESSIONS: dict[str, Session] = {}
LATEST: dict[str, Optional[str]] = {"sid": None}


def _session(sid: str) -> Session:
    if sid not in SESSIONS:
        raise HTTPException(404, "Unknown session")
    return SESSIONS[sid]


class Answer(BaseModel):
    text: str


class Confirm(BaseModel):
    edits: dict = {}


class CallRequest(BaseModel):
    scenario: str = "full"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/panels")
def panels():
    return FileResponse(STATIC / "panels.html")


@app.get("/api/status")
def status():
    return {
        "stt": speech.STT_AVAILABLE,
        "stt_model": speech.WHISPER_MODEL if speech.STT_AVAILABLE else None,
        "tts": speech.TTS_AVAILABLE,
        "ollama": ollama_available(),
        "llm_model": ollama_model(),
    }


@app.post("/api/session")
def new_session():
    sid = str(uuid.uuid4())
    s = Session(interview=InterviewSession(session_id=sid))
    SESSIONS[sid] = s
    LATEST["sid"] = sid
    return {"session_id": sid, **s.interview.start()}


def _handle_answer(sid: str, s: Session, text: str, spoken: bool = False) -> dict:
    result = s.interview.submit_answer(text, spoken=spoken)
    if result["type"] == "crisis" and result["kind"] == "red_flag":
        # Labels only. Never the content, never a severity.
        append_audit(f"session-{sid}", "system", said="Red flag detected; booking stopped",
                     heard=", ".join(result["flags"]))
    if result["type"] == "review":
        s.record = s.interview.build_record()
        result["record"] = s.record.to_dict()
        result["rows"] = s.interview.summary_rows()
    return result


@app.post("/api/session/{sid}/answer")
def answer(sid: str, body: Answer):
    s = _session(sid)
    if s.interview.finished:
        raise HTTPException(409, "Interview already finished")
    return _handle_answer(sid, s, body.text)


class ChangeRequest(BaseModel):
    key: str


@app.post("/api/session/{sid}/back")
def back(sid: str):
    s = _session(sid)
    try:
        result = s.interview.go_back()
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    s.record = None
    return result


@app.post("/api/session/{sid}/change")
def change(sid: str, body: ChangeRequest):
    s = _session(sid)
    try:
        result = s.interview.change(body.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    s.record = None
    return result


@app.post("/api/session/{sid}/audio")
async def audio(sid: str, file: UploadFile = File(...)):
    s = _session(sid)
    if not speech.STT_AVAILABLE:
        raise HTTPException(501, "Local speech-to-text not installed. Type instead.")
    data = await file.read()
    s.audio_bytes += len(data)
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        text = await run_in_threadpool(speech.transcribe, path)
    finally:
        Path(path).unlink(missing_ok=True)
    if not text:
        raise HTTPException(422, "We didn't hear anything. Please try again, a little closer to the microphone.")
    if s.interview.finished:
        raise HTTPException(409, "Interview already finished")
    return {"transcript": text, **_handle_answer(sid, s, text, spoken=True)}


_EDITABLE_CLINICAL = {"description", "duration", "trajectory", "taking_anything"}
_EDITABLE_PREFS = {"urgency", "appointment_type", "preferred_clinician"}


@app.post("/api/session/{sid}/confirm")
def confirm(sid: str, body: Confirm):
    s = _session(sid)
    if s.record is None or not s.interview.finished:
        raise HTTPException(409, "Interview not finished")
    r = s.record
    e = body.edits
    if "reason_for_contact" in e:
        r.reason_for_contact = e["reason_for_contact"].strip()
    for k in _EDITABLE_CLINICAL & e.keys():
        setattr(r.clinical_detail, k, e[k])
    for k in _EDITABLE_PREFS & e.keys():
        setattr(r.preferences, k, e[k] or None if k == "preferred_clinician" else e[k])
    if "times" in e:
        r.preferences.times = _split_list(e["times"])
    if "access_needs" in e:
        r.access_needs = _split_list(e["access_needs"])
    r.spoken_summary = build_spoken_summary(r.reason_for_contact, r.preferences)
    r.confirmed_by_patient = True
    save_record(r)
    if s.credential is None:
        s.credential = delegation.issue_credential()
    return {"record": r.to_dict(), "credential": s.credential, "practice": delegation.mock_practice_endpoint(s.credential)}


class SummaryPreview(BaseModel):
    reason: str = ""
    urgency: str = ""
    times: str = ""


@app.post("/api/summary-preview")
def summary_preview(body: SummaryPreview):
    prefs = Preferences(urgency=body.urgency, times=_split_list(body.times))
    return {"spoken_summary": build_spoken_summary(body.reason.strip() or "a general health matter", prefs)}


@app.get("/api/session/{sid}/speak")
def speak(sid: str, field: str = "spoken_summary"):
    s = _session(sid)
    if s.record is None:
        raise HTTPException(409, "No record yet")
    try:
        wav = speech.speak(s.record.to_dict(), field)
    except ValueError as exc:
        raise HTTPException(403, str(exc))
    if wav is None:
        return Response(status_code=204)
    return Response(wav, media_type="audio/wav")


@app.post("/api/session/{sid}/call")
def call(sid: str, body: CallRequest):
    s = _session(sid)
    if s.record is None:
        raise HTTPException(409, "No record yet")
    try:
        result = run_call(s.record, s.credential, body.scenario).to_dict()
    except CallRefused as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    s.last_call = result
    s.calls_spoken.extend(result["spoken_out"])
    return result


@app.post("/api/session/{sid}/revoke")
def revoke(sid: str):
    s = _session(sid)
    if not s.credential:
        raise HTTPException(409, "No credential to revoke")
    delegation.revoke_credential(s.credential["credential_id"])
    return {"credential": s.credential, "practice": delegation.mock_practice_endpoint(s.credential)}


@app.post("/api/session/{sid}/credential")
def reissue(sid: str):
    s = _session(sid)
    s.credential = delegation.issue_credential()
    return {"credential": s.credential, "practice": delegation.mock_practice_endpoint(s.credential)}


@app.get("/api/session/{sid}/audit")
def audit(sid: str):
    s = _session(sid)
    entries = get_audit(f"session-{sid}")
    if s.record:
        entries += get_audit(s.record.request_id)
    return {"entries": entries}


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode()) if not isinstance(value, str) else len(value.encode())


@app.get("/api/boundary")
def boundary(sid: Optional[str] = None, mode: str = "local"):
    sid = sid or LATEST["sid"]
    if not sid or sid not in SESSIONS:
        return {"session_id": None, "stays": [], "leaves": [], "bytes_out": 0, "fields_out": 0, "mode": mode}
    s = SESSIONS[sid]
    iv = s.interview
    record = s.record.to_dict() if s.record else None
    patient_turns = [t["text"] for t in iv.transcript if t["role"] == "patient"]

    stays = [
        {"label": "Her audio", "value": f"{s.audio_bytes:,} bytes, transcribed here then deleted" if s.audio_bytes else "None (typed input)"},
        {"label": "Full transcript", "value": iv.transcript},
        {"label": "Clinical detail", "value": record["clinical_detail"] if record else {k: v for k, v in iv.answers.items()}},
        {"label": "Structured record", "value": record or "Building..."},
        {"label": "Audit log", "value": f"{len(audit(sid)['entries'])} entries, on this device"},
    ]

    leaves: list[dict] = []
    if mode == "naive":
        if s.audio_bytes:
            leaves.append({"label": "Her voice recording", "dest": "Cloud speech-to-text", "value": f"{s.audio_bytes:,} bytes of biometric audio", "bytes": s.audio_bytes})
        if patient_turns:
            leaves.append({"label": f"Her words ({len(patient_turns)} answers)", "dest": "Cloud LLM API",
                           "value": iv.transcript, "bytes": _size(patient_turns)})
        if record:
            leaves.append({"label": "Clinical detail", "dest": "Cloud LLM API", "value": record["clinical_detail"], "bytes": _size(record["clinical_detail"])})
            leaves.append({"label": "Full record", "dest": "Vendor database", "value": record, "bytes": _size(record)})
            leaves.append({"label": "Spoken summary", "dest": "Cloud TTS", "value": record["spoken_summary"], "bytes": _size(record["spoken_summary"])})
    if record and record["confirmed_by_patient"]:
        leaves.append({"label": "Spoken summary", "dest": "Phone line, via carrier", "value": record["spoken_summary"], "bytes": _size(record["spoken_summary"])})
        leaves.append({"label": "Number dialled", "dest": "Telephony carrier", "value": PRACTICE_NUMBER, "bytes": _size(PRACTICE_NUMBER)})
    for line in s.calls_spoken:
        leaves.append({"label": "Agent line on call", "dest": "Phone line, via carrier", "value": line, "bytes": _size(line)})
    for line in s.live_spoken:
        leaves.append({"label": "Agent line on live call", "dest": "Phone line, via Twilio", "value": line, "bytes": _size(line)})

    return {
        "session_id": sid,
        "mode": mode,
        "crisis": iv.crisis["kind"] if iv.crisis else None,
        "stays": stays,
        "leaves": leaves,
        "bytes_out": sum(i["bytes"] for i in leaves),
        "fields_out": len(leaves),
    }


# ---------------- live phone calls (Twilio) ----------------

class PhoneConfigBody(BaseModel):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    from_number: Optional[str] = None
    public_url: Optional[str] = None


class LiveCallRequest(BaseModel):
    to: str


@app.get("/api/phone/config")
def phone_config():
    return telephony.status()


@app.post("/api/phone/config")
def set_phone_config(body: PhoneConfigBody):
    telephony.CONFIG.update(**body.model_dump())
    return telephony.status()


@app.post("/api/phone/tunnel")
async def start_tunnel():
    try:
        await run_in_threadpool(tunnel.start)
    except tunnel.TunnelError as exc:
        raise HTTPException(409, str(exc))
    return telephony.status()


@app.post("/api/session/{sid}/live-call")
async def start_live_call(sid: str, body: LiveCallRequest):
    s = _session(sid)
    if s.record is None:
        raise HTTPException(409, "No record yet")
    try:
        live = await run_in_threadpool(telephony.place_call, sid, s.record, s.credential, body.to)
    except CallRefused as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # Twilio REST errors carry a readable message
        raise HTTPException(502, f"Twilio refused the call: {getattr(exc, 'msg', None) or exc}")
    s.live_call_id = live.call_id
    return live.summary()


@app.get("/api/session/{sid}/live-call")
def live_call_state(sid: str):
    s = _session(sid)
    live = telephony.CALLS.get(s.live_call_id or "")
    if not live:
        raise HTTPException(404, "No live call for this request")
    return live.summary()


@app.post("/api/session/{sid}/live-call/hangup")
async def hang_up_live_call(sid: str):
    s = _session(sid)
    if not s.live_call_id:
        raise HTTPException(404, "No live call for this request")
    await run_in_threadpool(telephony.hang_up, s.live_call_id)
    return telephony.CALLS[s.live_call_id].summary()


async def _twilio_form(request: Request) -> dict:
    """Read a Twilio webhook and reject it unless Twilio signed it."""
    from twilio.request_validator import RequestValidator

    form = dict(await request.form())
    token = telephony.CONFIG.auth_token
    url = f"{telephony.CONFIG.public_url}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    signature = request.headers.get("x-twilio-signature", "")
    if not token or not signature or not RequestValidator(token).validate(url, form, signature):
        raise HTTPException(403, "Invalid Twilio signature")
    return form


def _live(call_id: str) -> telephony.LiveCall:
    live = telephony.CALLS.get(call_id)
    if not live:
        raise HTTPException(404, "Unknown call")
    return live


def _twiml(xml: str) -> Response:
    return Response(xml, media_type="application/xml")


def _record_agent_line(live: telephony.LiveCall, text: str) -> None:
    live.agent_line(text)
    if live.session_id in SESSIONS:
        SESSIONS[live.session_id].live_spoken.append(text)


@app.post("/twilio/answer/{call_id}")
async def twilio_answer(call_id: str, request: Request):
    await _twilio_form(request)
    live = _live(call_id)
    received = time.monotonic()
    live.status = "in-progress"
    live.answered_at = received
    opening = live.brain.opening()
    xml = await run_in_threadpool(telephony.twiml_gather, call_id, opening)
    live.opening_latency = round(time.monotonic() - received, 2)
    _record_agent_line(live, opening)
    return _twiml(xml)


@app.post("/twilio/heard/{call_id}")
async def twilio_heard(call_id: str, request: Request):
    form = await _twilio_form(request)
    live = _live(call_id)
    heard = (form.get("SpeechResult") or "").strip()
    if live.ended:
        return _twiml(telephony.twiml_hangup())
    if heard:
        live.event("practice", "speech", heard)
        reply, end = live.brain.respond(heard)
    else:
        reply, end = live.brain.silence()
    _record_agent_line(live, reply)
    if end:
        xml = await run_in_threadpool(telephony.twiml_goodbye, reply)
        await run_in_threadpool(live.finish)
        return _twiml(xml)
    return _twiml(await run_in_threadpool(telephony.twiml_gather, call_id, reply))


@app.post("/twilio/status/{call_id}")
async def twilio_status(call_id: str, request: Request):
    form = await _twilio_form(request)
    live = _live(call_id)
    status_value = form.get("CallStatus", "")
    if status_value:
        live.status = status_value
        live.event("system", "status", f"Call {status_value.replace('-', ' ')}")
    if status_value in telephony.ENDED_STATUSES:
        live.ended = True
        await run_in_threadpool(live.finish)
    return Response(status_code=204)


_TTS_NAME = re.compile(r"^[0-9a-f]{64}\.wav$")


@app.get("/twilio/audio/{name}")
def twilio_audio(name: str):
    if not _TTS_NAME.match(name):
        raise HTTPException(404, "Not found")
    path = speech.TTS_DIR / name
    if not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="audio/wav")
