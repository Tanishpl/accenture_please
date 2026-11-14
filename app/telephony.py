"""Live phone calls through Twilio, turn by turn.

Twilio is only the carrier. The agent's voice is generated on this device
(app/speech.py) and played with <Play>; it never uses <Say>, which would be
cloud speech synthesis. Twilio does transcribe the receptionist's side, as
the carrier of that call. Nothing the patient said or recorded is sent.

Turn-by-turn <Gather input="speech"> is used rather than media streams
because Twilio trial accounts block <Stream>.
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app import speech
from app.record import append_audit

try:
    from twilio.rest import Client
    from twilio.twiml.voice_response import Gather, VoiceResponse
    TWILIO_LIB = True
except ImportError:
    TWILIO_LIB = False

_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = _ROOT / ".env" if (_ROOT / ".env").exists() else _ROOT / "env"
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
ENDED_STATUSES = {"completed", "no-answer", "busy", "failed", "canceled"}


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass
class PhoneConfig:
    """Held in memory only. UI overrides are never written to disk."""
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    public_url: str = ""
    tunnel: str = "none"

    @classmethod
    def load(cls) -> "PhoneConfig":
        file_values = _read_env_file(ENV_FILE)
        if ENV_FILE.name == "env":
            # The legacy `env` file belongs to the older ngrok setup; its tunnel URL is usually dead.
            file_values.pop("PUBLIC_URL", None)
        get = lambda k: os.environ.get(k) or file_values.get(k, "")
        return cls(
            account_sid=get("TWILIO_ACCOUNT_SID"),
            auth_token=get("TWILIO_AUTH_TOKEN"),
            from_number=get("TWILIO_PHONE_NUMBER"),
            public_url=get("PUBLIC_URL").rstrip("/"),
            tunnel="PUBLIC_URL" if get("PUBLIC_URL") else "none",
        )

    def update(self, **values: Optional[str]) -> None:
        for key in ("account_sid", "auth_token", "from_number", "public_url"):
            value = (values.get(key) or "").strip()
            if value:
                setattr(self, key, value.rstrip("/") if key == "public_url" else value)
                if key == "public_url":
                    self.tunnel = "manual"

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)


CONFIG = PhoneConfig.load()
CALLS: dict[str, "LiveCall"] = {}


def status() -> dict:
    problems = []
    if not TWILIO_LIB:
        problems.append("The twilio Python package is not installed (pip install twilio).")
    if not CONFIG.account_sid or not CONFIG.auth_token:
        problems.append("Enter your Twilio Account SID and Auth Token.")
    if not CONFIG.from_number:
        problems.append("Enter your Twilio phone number (the number calls come from).")
    elif not E164.match(CONFIG.from_number):
        problems.append("The Twilio phone number must be in international format, e.g. +441234567890.")
    if not CONFIG.public_url:
        problems.append("Twilio needs a public URL to reach this laptop. Start a tunnel or set PUBLIC_URL.")
    if not speech.local_tts_available():
        problems.append("No local voice is available on this device to speak the agent's lines.")
    sid = CONFIG.account_sid
    return {
        "configured": CONFIG.configured,
        "account_sid_hint": f"AC…{sid[-4:]}" if sid else None,
        "from_number": CONFIG.from_number or None,
        "public_url": CONFIG.public_url or None,
        "tunnel": CONFIG.tunnel,
        "tts": speech.local_tts_available(),
        "twilio_lib": TWILIO_LIB,
        "problems": problems,
        "ready": not problems,
    }


def _client() -> "Client":
    return Client(CONFIG.account_sid, CONFIG.auth_token)


# ---------- TwiML ----------

def audio_url(text: str) -> str:
    path = speech.synthesise_to_file(text)
    return f"{CONFIG.public_url}/twilio/audio/{path.stem}.wav"


def twiml_gather(call_id: str, text: str) -> str:
    """Play a locally generated line, then listen for the receptionist."""
    response = VoiceResponse()
    gather = Gather(
        input="speech",
        language="en-GB",
        speech_model="phone_call",
        speech_timeout="auto",
        timeout=6,
        action_on_empty_result=True,
        action=f"{CONFIG.public_url}/twilio/heard/{call_id}",
        method="POST",
    )
    gather.play(audio_url(text))
    response.append(gather)
    # Only reached if Gather somehow returns without posting.
    response.redirect(f"{CONFIG.public_url}/twilio/heard/{call_id}", method="POST")
    return str(response)


def twiml_goodbye(text: str) -> str:
    response = VoiceResponse()
    response.play(audio_url(text))
    response.hangup()
    return str(response)


def twiml_hangup() -> str:
    response = VoiceResponse()
    response.hangup()
    return str(response)


# ---------- live calls ----------

@dataclass
class LiveCall:
    call_id: str
    session_id: str
    request_id: str
    to_number: str
    brain: Any
    twilio_sid: Optional[str] = None
    status: str = "queued"
    started: float = field(default_factory=time.monotonic)
    answered_at: Optional[float] = None
    opening_latency: Optional[float] = None
    events: list[dict] = field(default_factory=list)
    ended: bool = False
    finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def event(self, speaker: str, kind: str, text: str) -> None:
        self.events.append({"t": round(time.monotonic() - self.started, 1), "speaker": speaker, "kind": kind, "text": text})

    def agent_line(self, text: str) -> None:
        # The brain writes the audit entry for each line; this is the live timeline.
        self.event("agent", "speech", text)

    def finish(self) -> None:
        with self.lock:
            if self.finished:
                return
            self.finished = True
        self.brain.finish(reason=f"live call, Twilio status {self.status}")

    def summary(self) -> dict:
        return {
            "call_id": self.call_id,
            "status": self.status,
            "events": list(self.events),
            "outcome": getattr(self.brain, "outcome", None),
            "agreed": getattr(self.brain, "agreed", None),
            "unanswered": list(getattr(self.brain, "unanswered", []) or []),
            "ended": self.ended,
            "opening_latency": self.opening_latency,
            "to": mask_number(self.to_number),
        }


def mask_number(number: str) -> str:
    return number[:4] + "•" * max(0, len(number) - 7) + number[-3:] if len(number) > 7 else number


def place_call(session_id: str, record, credential: Optional[dict], to_number: str, brain_factory=None) -> LiveCall:
    from app.call import CallRefused, preflight

    preflight(record, credential)
    to_number = re.sub(r"[\s()-]", "", to_number or "")
    if not E164.match(to_number):
        raise CallRefused("Enter the number in international format, e.g. +447700900123.")
    problems = status()["problems"]
    if problems:
        raise CallRefused(" ".join(problems))

    if brain_factory is None:
        from app.call import CallBrain
        brain_factory = lambda r: CallBrain(r, max_turns=8, live=True)
    brain = brain_factory(record)

    call = LiveCall(call_id=uuid.uuid4().hex, session_id=session_id,
                    request_id=record.request_id, to_number=to_number, brain=brain)
    from app.call import FALLBACK, LIVE_OPENING_LINE
    speech.warm_phrases([LIVE_OPENING_LINE, *FALLBACK.values()])

    append_audit(record.request_id, "system",
                 said=f"Dial attempt (live) to {mask_number(to_number)} from {CONFIG.from_number}")
    call.event("system", "status", f"Dialling {mask_number(to_number)}")
    created = _client().calls.create(
        to=to_number,
        from_=CONFIG.from_number,
        url=f"{CONFIG.public_url}/twilio/answer/{call.call_id}",
        status_callback=f"{CONFIG.public_url}/twilio/status/{call.call_id}",
        # Trial accounts reject timeout, method and other events. Defaults: POST, rings 60s.
        status_callback_event=["answered", "completed"],
    )
    call.twilio_sid = created.sid
    CALLS[call.call_id] = call
    return call


def hang_up(call_id: str) -> None:
    call = CALLS.get(call_id)
    if not call or call.ended or not call.twilio_sid:
        return
    _client().calls(call.twilio_sid).update(status="completed")
    call.event("system", "status", "Hung up from the app")
