"""The voice agent's brain: an ElevenLabs agent in text-only mode.

One ElevenLabs conversation websocket per call, opened when the receptionist
answers. Each phrase Twilio transcribes goes in as a `user_message`; the
agent's text replies and client tool calls (record_booking, connect_patient,
report_outcome, hang_up) come back and become the next TwiML. The agent is
created by scripts/setup_agent.py.

Replies arrive in well under a second, inside Twilio's 5-second TwiML deadline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
import websockets

from .config import settings
from .models import PatientRequest
from .voice import strip_tone_tags

log = logging.getLogger(__name__)

SIGNED_URL = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"

# Spoken by the app itself, so taking over costs no time. The agent's prompt
# (scripts/setup_agent.py) says it has already said this.
OPENER = (
    "Hello, I'm an automated assistant calling on behalf of {patient_name}, date of birth "
    "{date_of_birth}. I'd like to book them a GP appointment, please. Are you able to help?"
)
FALLBACK_SAY = "Sorry, I didn't catch that. Could you say it again, please?"

# After a text reply, wait this long for a tool call that belongs to the same turn.
TOOL_GRACE_SECONDS = 0.35
# Tools that end our part of the call: narration after them is dropped.
FINAL_TOOLS = {"hang_up", "connect_patient"}

TOOL_RESULTS = {
    "record_booking": "Booking saved and the patient has been texted. Don't repeat the details. "
    "Ask if there's anything else, or say goodbye and call hang_up.",
    "report_outcome": "Noted; the patient will be told.",
    "connect_patient": "Calling the patient now.",
    "hang_up": "Call ended.",
}


@dataclass
class Reply:
    say: str
    next: str = "listen"  # listen | connect_patient | hang_up
    booking: dict | None = None
    outcome: str | None = None
    error: str | None = None  # why the agent couldn't reply (shown in the call log)
    tools: list[str] = field(default_factory=list)


def _spoken_phone(e164: str) -> str:
    """'+447700900123' -> '07700 900123', which reads naturally aloud."""
    if e164.startswith("+44"):
        national = "0" + e164[3:]
        return f"{national[:5]} {national[5:]}"
    return e164


class ReceptionistAgent:
    def __init__(self, patient: PatientRequest) -> None:
        self.details = patient.dynamic_variables()
        self.details["patient_phone"] = _spoken_phone(patient.patient_phone)
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._ws = None
        self._ready = asyncio.Event()
        self._error: str | None = None
        self._task: asyncio.Task | None = None
        self._partial: Reply | None = None  # the turn in progress, kept if it times out

    # ---------- connection ----------

    def opener(self, heard: str) -> str:
        """Start connecting in the background and return the fixed first line."""
        self._task = asyncio.create_task(self._run())
        return OPENER.format(**self.details)

    async def _run(self) -> None:
        try:
            await self._connect()
        except Exception as exc:
            self._error = f"couldn't reach the ElevenLabs agent ({exc})"
            log.error("ElevenLabs agent: %s", self._error)
        finally:
            self._ws = None
            self._error = self._error or "the ElevenLabs conversation closed"
            self._ready.set()

    async def _connect(self) -> None:
        if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id):
            raise RuntimeError("set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID (run scripts/setup_agent.py)")
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                SIGNED_URL,
                params={"agent_id": settings.elevenlabs_agent_id},
                headers={"xi-api-key": settings.elevenlabs_api_key},
            )
            r.raise_for_status()
        async with websockets.connect(r.json()["signed_url"]) as ws:
            await ws.send(json.dumps({
                "type": "conversation_initiation_client_data",
                "dynamic_variables": self.details,
            }))
            self._ws = ws
            self._ready.set()
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "event_id": msg["ping_event"]["event_id"]}))
                elif kind in {"agent_response", "client_tool_call"}:
                    self._events.put_nowait(msg)
                elif kind == "client_error":
                    log.warning("ElevenLabs agent error: %s", msg)

    async def close(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    # ---------- one turn ----------

    async def reply(self, heard: str, turns_left: int | None) -> Reply:
        if turns_left is None:
            note = None
        elif turns_left <= 0:
            note = ("This is your last reply: the line will drop straight after it. Wrap up now: "
                    "confirm any agreed slot with record_booking, otherwise use report_outcome, "
                    "then say goodbye and call hang_up.")
        else:
            note = f"The phone line allows {turns_left} more replies after this one."
        started = time.monotonic()
        try:
            reply = await asyncio.wait_for(self._turn(heard, note), settings.reply_timeout_seconds)
        except TimeoutError:
            reply = self._partial  # keep whatever was said or recorded before the deadline
            self._error = f"the agent took longer than {settings.reply_timeout_seconds}s"
        if reply is None or not reply.say:
            error = self._error or "the agent gave no reply"
            log.warning("Agent turn failed after %.1fs: %s", time.monotonic() - started, error)
            fallback = Reply(say=FALLBACK_SAY, error=error)
            if reply is not None:  # tools ran but nothing was said: keep their effects
                fallback.next, fallback.booking, fallback.outcome = reply.next, reply.booking, reply.outcome
            return fallback
        return reply

    async def _turn(self, heard: str, note: str | None) -> Reply | None:
        await self._ready.wait()
        ws = self._ws
        if ws is None:
            return None
        # Drop anything the agent said on its own since the last turn.
        while not self._events.empty():
            self._events.get_nowait()
        if note:
            await ws.send(json.dumps({"type": "contextual_update", "text": note}))
        await ws.send(json.dumps({"type": "user_message", "text": heard}))

        reply = self._partial = Reply(say="")
        said: list[str] = []
        waiting_for_text = True  # until the agent speaks after its latest tool call
        while True:
            try:
                if waiting_for_text:
                    msg = await self._events.get()
                else:
                    msg = await asyncio.wait_for(self._events.get(), TOOL_GRACE_SECONDS)
            except TimeoutError:
                break  # nothing more for this turn

            if msg["type"] == "agent_response":
                text = strip_tone_tags(msg["agent_response_event"]["agent_response"].strip())
                if reply.next != "listen" and said:
                    break  # narration after hang_up / connect_patient: don't speak it
                if text:
                    said.append(text)
                    reply.say = " ".join(said)
                if reply.next != "listen":
                    break
                waiting_for_text = False
                continue

            call = msg["client_tool_call"]
            name, params = call["tool_name"], call.get("parameters") or {}
            reply.tools.append(name)
            self._apply_tool(reply, name, params)
            await ws.send(json.dumps({
                "type": "client_tool_result",
                "tool_call_id": call["tool_call_id"],
                "result": TOOL_RESULTS.get(name, f"Unknown tool {name}"),
                "is_error": name not in TOOL_RESULTS,
            }))
            if name in FINAL_TOOLS and said:
                break
            waiting_for_text = True
        return reply

    @staticmethod
    def _apply_tool(reply: Reply, name: str, params: dict) -> None:
        if name == "record_booking":
            reply.booking = {k: str(params.get(k, "")) for k in ("date", "time", "clinician", "location", "notes")}
        elif name == "report_outcome":
            reply.outcome = str(params.get("summary", ""))
        elif name == "connect_patient":
            reply.next = "connect_patient"
        elif name == "hang_up":
            reply.next = "hang_up"
