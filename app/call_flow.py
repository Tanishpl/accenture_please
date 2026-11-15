"""What happens each time Twilio hears a phrase on the call.

hold:  classify the phrase (queue message / IVR menu / closed / a person).
       A person -> the agent says its opening line, and we move to 'agent'.
agent: the ElevenLabs agent writes the reply; we record bookings and outcomes, and either
       keep listening, connect the patient, or hang up.

Every function returns the TwiML Twilio should run next.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import telephony
from .agent import ReceptionistAgent, Reply
from .classifier import Heard, HoldClassifier
from .config import settings
from .models import TERMINAL, Booking, CallSession, CallStatus
from .voice import strip_tone_tags

log = logging.getLogger(__name__)

_background: set[asyncio.Task] = set()


def in_background(coro) -> None:
    """Run work (SMS, dialling the patient) without delaying the TwiML response."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def call_answered(session: CallSession) -> str:
    if session.phase == "dialling":
        session.phase = "hold"
        session.status = CallStatus.ON_HOLD
        session.classifier = HoldClassifier(session.patient.surgery_name, session.patient.reason)
        session.log("system", "Call connected. Listening to the queue.")
    return telephony.twiml_hold(session.id)


async def heard(session: CallSession, text: str) -> str:
    session.hops += 1
    text = text.strip()
    if session.phase == "done":
        return telephony.twiml_hangup()
    if session.phase == "agent":
        return await _agent_turn(session, text)
    return await _hold_turn(session, text)


async def _hold_turn(session: CallSession, text: str) -> str:
    if session.takeover_requested:
        session.log("heard", "manual takeover requested")
        return take_over(session, text, "manual override")
    if session.pending_digits:
        digits, session.pending_digits = session.pending_digits, None
        session.log("heard", "pending phone menu action")
        return press_digits(session, digits)

    classifier: HoldClassifier = session.classifier
    try:
        verdict = await asyncio.wait_for(classifier.classify(text), settings.reply_timeout_seconds)
    except Exception as exc:  # timeout or anything else: never fail the webhook mid-call
        log.warning("Hold classifier failed (%r); using keyword rules", exc)
        verdict = classifier.keyword_rules(text)
    session.log("heard", f"[{verdict.heard.value}] {verdict.reason}")

    if verdict.heard == Heard.HUMAN:
        return take_over(session, text, verdict.reason)
    if verdict.heard == Heard.CLOSED:
        return give_up(session, "The surgery is closed or its queue is full.",
                       f"We couldn't get through to {session.patient.surgery_name}: {verdict.reason}. "
                       "Please try again during opening hours.")
    if session.turns_left() == 0:
        return give_up(session, "Still in the queue when the trial line limit was reached, so we hung up.")
    if verdict.heard == Heard.IVR_MENU and verdict.digits:
        return press_digits(session, verdict.digits)
    return telephony.twiml_hold(session.id)


def press_digits(session: CallSession, digits: str) -> str:
    session.log("dtmf", f"Pressed {digits}")
    return telephony.twiml_hold(session.id, digits=digits)


def take_over(session: CallSession, heard_text: str, why: str) -> str:
    """The receptionist answered: the agent introduces itself."""
    session.phase = "agent"
    session.status = CallStatus.TALKING
    session.answered_at = time.time()
    session.log("system", f"Receptionist answered ({why}). Agent is talking.")
    session.agent = ReceptionistAgent(session.patient)
    opener = strip_tone_tags(session.agent.opener(heard_text))
    session.log("agent", opener)
    return telephony.twiml_reply(session.id, opener)


async def _agent_turn(session: CallSession, text: str) -> str:
    if settings.show_transcripts:
        session.log("receptionist", text)
    else:
        session.log("receptionist", "receptionist turn received")
    agent: ReceptionistAgent = session.agent
    reply: Reply = await agent.reply(text, session.turns_left())
    reply.say = strip_tone_tags(reply.say)
    if reply.error:
        session.log("system", f"Agent couldn't reply: {reply.error}")
    session.log("agent", reply.say if settings.show_transcripts else "agent reply generated")

    if reply.booking:
        record_booking(session, reply.booking)
    if reply.outcome:
        session.outcome = reply.outcome
        session.log("outcome", reply.outcome)

    if reply.next == "connect_patient" and session.patient.allow_patient_callback:
        in_background(agent.close())
        session.phase = "done"
        session.status = CallStatus.HANDED_TO_PATIENT
        session.outcome = "Patient connected directly to the receptionist."
        session.log("system", "Receptionist asked to speak to the patient. Calling them now.")
        in_background(telephony.call_patient(session.id, session.patient.patient_phone))
        return telephony.twiml_conference(session.id, starts_conference=False, intro=reply.say)

    if reply.next == "hang_up" or session.turns_left() == 0:
        finish(session)
        return telephony.twiml_goodbye(reply.say)
    return telephony.twiml_reply(session.id, reply.say)


def record_booking(session: CallSession, data: dict) -> None:
    session.booking = b = Booking(
        date=str(data.get("date", "")),
        time=str(data.get("time", "")),
        clinician=str(data.get("clinician", "")),
        location=str(data.get("location", "")),
        notes=str(data.get("notes", "")),
    )
    session.status = CallStatus.BOOKED
    session.outcome = "Appointment booked."
    session.log("booking", " ".join(x for x in (b.date, b.time, b.clinician, b.location) if x))
    sms = f"Booked at {session.patient.surgery_name}: {b.date} at {b.time}"
    if b.clinician:
        sms += f" with {b.clinician}"
    if b.location:
        sms += f" ({b.location})"
    if b.notes:
        sms += f". Note: {b.notes}"
    in_background(telephony.send_sms(session.patient.patient_phone, sms))


def give_up(session: CallSession, outcome: str, sms: str | None = None) -> str:
    session.phase = "done"
    session.status = CallStatus.FAILED
    session.outcome = outcome
    session.log("system", outcome)
    in_background(telephony.send_sms(session.patient.patient_phone, sms or outcome))
    return telephony.twiml_hangup()


def finish(session: CallSession) -> None:
    """The call is over (we said goodbye, or Twilio reports it completed). Idempotent."""
    if session.phase == "done":
        return
    if session.agent is not None:
        in_background(session.agent.close())
    was_talking = session.phase == "agent"
    session.phase = "done"
    if session.status not in TERMINAL:
        session.status = CallStatus.ENDED
        session.outcome = session.outcome or "The call ended before a booking was made."
    if was_talking and not session.booking:
        in_background(telephony.send_sms(
            session.patient.patient_phone,
            f"Your call to {session.patient.surgery_name} ended without a booking. {session.outcome}",
        ))
