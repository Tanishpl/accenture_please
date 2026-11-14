"""FastAPI app. Serves the UI and API on localhost only.

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import delegation, speech
from app.call import CallRefused, run_call
from app.interview import InterviewSession, build_spoken_summary, ollama_available
from app.record import Record, append_audit, get_audit, init_db, save_record
from mock.receptionist import PRACTICE_NUMBER

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="GP booking agent (local-first)")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
init_db()


@dataclass
class Session:
    interview: InterviewSession
    record: Optional[Record] = None
    credential: Optional[dict] = None
    audio_bytes: int = 0
    last_call: Optional[dict] = None
    calls_spoken: list[str] = field(default_factory=list)


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
    return {"stt": speech.STT_AVAILABLE, "tts": speech.TTS_AVAILABLE, "ollama": ollama_available()}


@app.post("/api/session")
def new_session():
    sid = str(uuid.uuid4())
    s = Session(interview=InterviewSession(session_id=sid))
    SESSIONS[sid] = s
    LATEST["sid"] = sid
    return {"session_id": sid, **s.interview.start()}


def _handle_answer(sid: str, s: Session, text: str) -> dict:
    result = s.interview.submit_answer(text)
    if result["type"] == "crisis" and result["kind"] == "red_flag":
        # Labels only. Never the content, never a severity.
        append_audit(f"session-{sid}", "system", said="Red flag detected; booking stopped",
                     heard=", ".join(result["flags"]))
    if result["type"] == "review":
        s.record = s.interview.build_record()
    return result


@app.post("/api/session/{sid}/answer")
def answer(sid: str, body: Answer):
    s = _session(sid)
    if s.interview.finished:
        raise HTTPException(409, "Interview already finished")
    return _handle_answer(sid, s, body.text)


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
        text = speech.transcribe(path)
    finally:
        Path(path).unlink(missing_ok=True)
    return {"transcript": text, **_handle_answer(sid, s, text)}


_EDITABLE_CLINICAL = {"description", "duration", "trajectory", "taking_anything"}
_EDITABLE_PREFS = {"urgency", "appointment_type", "preferred_clinician"}


@app.post("/api/session/{sid}/confirm")
def confirm(sid: str, body: Confirm):
    s = _session(sid)
    if s.record is None:
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
        r.preferences.times = [t.strip() for t in e["times"].split(";") if t.strip()]
    if "access_needs" in e:
        r.access_needs = [t.strip() for t in e["access_needs"].split(";") if t.strip()]
    r.spoken_summary = build_spoken_summary(r.reason_for_contact, r.preferences)
    r.confirmed_by_patient = True
    save_record(r)
    if s.credential is None:
        s.credential = delegation.issue_credential()
    return {"record": r.to_dict(), "credential": s.credential, "practice": delegation.mock_practice_endpoint(s.credential)}


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
        {"label": "Her audio", "value": f"{s.audio_bytes:,} bytes recorded" if s.audio_bytes else "None (typed input)"},
        {"label": "Full transcript", "value": iv.transcript},
        {"label": "Clinical detail", "value": record["clinical_detail"] if record else {k: v for k, v in iv.answers.items()}},
        {"label": "Structured record", "value": record or "Building..."},
        {"label": "Audit log", "value": f"{len(audit(sid)['entries'])} entries, on this device"},
    ]

    leaves: list[dict] = []
    if mode == "naive":
        if s.audio_bytes:
            leaves.append({"label": "Her voice recording", "dest": "Cloud speech-to-text", "value": f"{s.audio_bytes:,} bytes of biometric audio", "bytes": s.audio_bytes})
        for turn in patient_turns:
            leaves.append({"label": "Transcript turn", "dest": "Cloud LLM API", "value": turn, "bytes": _size(turn)})
        if record:
            leaves.append({"label": "Clinical detail", "dest": "Cloud LLM API", "value": record["clinical_detail"], "bytes": _size(record["clinical_detail"])})
            leaves.append({"label": "Full record", "dest": "Vendor database", "value": record, "bytes": _size(record)})
            leaves.append({"label": "Spoken summary", "dest": "Cloud TTS", "value": record["spoken_summary"], "bytes": _size(record["spoken_summary"])})
    if record and record["confirmed_by_patient"]:
        leaves.append({"label": "Spoken summary", "dest": "Phone line, via carrier", "value": record["spoken_summary"], "bytes": _size(record["spoken_summary"])})
        leaves.append({"label": "Number dialled", "dest": "Telephony carrier", "value": PRACTICE_NUMBER, "bytes": _size(PRACTICE_NUMBER)})
    for line in s.calls_spoken:
        leaves.append({"label": "Agent line on call", "dest": "Phone line, via carrier", "value": line, "bytes": _size(line)})

    return {
        "session_id": sid,
        "mode": mode,
        "crisis": iv.crisis["kind"] if iv.crisis else None,
        "stays": stays,
        "leaves": leaves,
        "bytes_out": sum(i["bytes"] for i in leaves),
        "fields_out": len(leaves),
    }
