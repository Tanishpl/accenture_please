"""Agent turn logic for the call, and the fallback table.

No real telephony: the practice side is mock/receptionist.py. Every line
the agent speaks is either a fixed script line or built from an
allowlisted record field via speakable_text(), so clinical detail cannot
reach the call even by accident.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import delegation
from app.record import Record, _connect, append_audit
from app.interview import join_or, with_article
from app.speech import speakable_text
from mock import receptionist

MIN_RING_SECONDS = 15
MAX_ATTEMPTS_PER_DAY = 3
RETRY_MINUTES = 15
MAX_UNPARSED_TURNS = 3

OPENING_LINE = (
    "Hello, this is an automated assistant calling on behalf of a registered "
    "patient. She would like to book an appointment. I have her answers ready, "
    "and I can connect her directly if you need to speak to her."
)

FALLBACK = {
    "not_in_record": "I do not have that answer. I can ask her and ring back, or connect her now if she is free.",
    "clinical": "I cannot answer that, I am not clinical. I can pass on her description or connect her.",
    "identity": "I cannot confirm her identity, I am an assistant. She can confirm that herself. Shall I connect her?",
    "decline": "Understood, I will ask her to call you directly.",
    "unparsed_limit": "Sorry, I am having trouble following. I can connect her to you now, or call back later. Which would you prefer?",
    "unparsed": "Sorry, I did not catch that. Could you say it again?",
}

_INTENTS: list[tuple[str, re.Pattern]] = [
    ("decline", re.compile(r"(don'?t|do not|can'?t|cannot|won'?t) (take|accept|deal with|speak to).{0,30}(automated|ai|robot|machine|bot)|(automated|ai|robot|machine|bot).{0,40}(ring|call) (herself|themselves)", re.I)),
    ("identity", re.compile(r"date of birth|\bdob\b|(?<!email )address|postcode|nhs number|confirm (her|their) (identity|details)|security question", re.I)),
    ("clinical", re.compile(r"sound like|should (she|he|they) be seen|needs? to be seen|how (bad|serious|severe)|symptoms|pain (score|level)|is it serious|red flag", re.I)),
    ("confirm", re.compile(r"\b(booked|confirmed|that'?s right|all done|see her then)\b", re.I)),
    ("offer", re.compile(r"\b(\d{1,2}[:.]\d{2}|\d{1,2}\s?(am|pm))\b", re.I)),
    ("reason", re.compile(r"what'?s it (about|for|regarding)|what is it (about|for|regarding)|reason for", re.I)),
    ("urgency", re.compile(r"how (soon|urgent)|when (does|would) she (want|like) to be seen", re.I)),
    ("appointment_type", re.compile(r"come in|face to face|in person|phone (call|appointment)|telephone", re.I)),
    ("times", re.compile(r"when suits|what (days|times)|availab|when (is|would be) (good|best)|when can she", re.I)),
    ("clinician", re.compile(r"particular (doctor|gp|nurse|clinician)|which (doctor|gp|clinician)|preference for (a )?(doctor|gp)", re.I)),
]
_QUESTION_LIKE = re.compile(r"\?\s*$|^(what|can|could|does|do|is|are|has|have|will|would|when|where|who|how)\b", re.I)
_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b", re.I)
_TIME = re.compile(r"\b(\d{1,2}[:.]\d{2}|\d{1,2}\s?(am|pm))\b", re.I)
_WITH = re.compile(r"\bwith (dr\.? \w+|the \w+( \w+)?|nurse \w+)", re.I)


class CallRefused(Exception):
    pass


def classify(utterance: str) -> str:
    for intent, pattern in _INTENTS:
        if pattern.search(utterance):
            return intent
    return "unknown_question" if _QUESTION_LIKE.search(utterance.strip()) else "unparsed"


def parse_offer(utterance: str) -> str:
    bits = []
    if (d := _DAY.search(utterance)):
        bits.append(d.group(0).capitalize())
    if (t := _TIME.search(utterance)):
        bits.append(f"at {t.group(0)}")
    if (w := _WITH.search(utterance)):
        bits.append(w.group(0))
    return " ".join(bits)


def answer_from_record(intent: str, record: dict) -> Optional[str]:
    """Build an answer only from speakable fields. None means 'not in record'."""
    prefs = record["preferences"]
    if intent == "reason":
        return f"It is about {with_article(speakable_text(record, 'reason_for_contact'))}."
    if intent == "urgency":
        return f"She would like to be seen {prefs['urgency']}." if prefs.get("urgency") else None
    if intent == "appointment_type":
        return f"She would prefer {prefs['appointment_type']}." if prefs.get("appointment_type") else None
    if intent == "times":
        return f"{join_or(prefs['times']).capitalize()} would suit her best." if prefs.get("times") else None
    if intent == "clinician":
        speakable_text(record, "preferences")  # same boundary check
        return f"She would like to see {prefs['preferred_clinician']}." if prefs.get("preferred_clinician") else "She has no preference."
    return None


def attempts_today(request_id: str) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE request_id = ? AND speaker = 'system' "
            "AND said LIKE 'Dial attempt%' AND timestamp LIKE ?",
            (request_id, f"{today}%"),
        ).fetchone()
    return row[0]


@dataclass
class CallResult:
    outcome: str
    events: list[dict] = field(default_factory=list)
    spoken_out: list[str] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    agreed: Optional[str] = None
    next_attempt: Optional[str] = None
    opening_latency: Optional[float] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def run_call(record: Record, credential: Optional[dict], scenario: str = "full") -> CallResult:
    rec = record.to_dict()
    rid = record.request_id

    if not record.confirmed_by_patient:
        raise CallRefused("The patient has not confirmed the record.")
    if not credential or delegation.mock_practice_endpoint(credential)["status"] != "authorised":
        raise CallRefused("No valid delegation credential. The patient may have revoked it.")
    if record.red_flags:
        raise CallRefused("Red flags were detected. This must not be booked as a routine call.")
    if attempts_today(rid) >= MAX_ATTEMPTS_PER_DAY:
        raise CallRefused(f"Daily limit of {MAX_ATTEMPTS_PER_DAY} attempts reached for this request.")

    res = CallResult(outcome="in_progress")
    clock = 0.0

    def event(speaker: str, kind: str, text: str, dt: float = 1.0) -> None:
        nonlocal clock
        clock += dt
        res.events.append({"t": round(clock, 1), "speaker": speaker, "kind": kind, "text": text})

    def agent_says(text: str, heard: str = "", dt: float = 1.2, unanswered: str = "", agreed: str = "") -> None:
        event("agent", "speech", text, dt)
        res.spoken_out.append(text)
        append_audit(rid, "agent", said=text, heard=heard, unanswered=unanswered, agreed=agreed)

    n = attempts_today(rid) + 1
    dial_note = f"Dial attempt {n} of {MAX_ATTEMPTS_PER_DAY} to {receptionist.PRACTICE_NUMBER} (ring at least {MIN_RING_SECONDS}s)"
    event("system", "dial", dial_note, 0)
    append_audit(rid, "system", said=dial_note)

    human_on_line = False
    unparsed_streak = 0
    pending_offer: Optional[str] = None

    for kind, line in receptionist.script(scenario):
        if kind == "ivr":
            event("practice", "ivr", line, 2)
            event("agent", "dtmf", "[keypad tone: 1]", 1.5)
            append_audit(rid, "system", said="DTMF 1", heard=line)
            continue
        if kind == "hold":
            event("practice", "hold", line, 20 if "music" in line else 3)
            event("agent", "silent", "[silent during hold]", 0)
            continue
        if kind == "voicemail":
            event("practice", "voicemail", line, 2)
            event("system", "hangup", "Voicemail or closed. Hung up without leaving a message.", 1)
            append_audit(rid, "system", said="Hung up: voicemail/closed", heard=line)
            if n >= MAX_ATTEMPTS_PER_DAY:
                res.outcome = "attempt_cap_reached"
            else:
                res.outcome = "retry_scheduled"
                res.next_attempt = (datetime.now(timezone.utc) + timedelta(minutes=RETRY_MINUTES)).isoformat()
            return res

        event("practice", "speech", line, 2.5)

        if not human_on_line:
            human_on_line = True
            agent_says(OPENING_LINE, heard=line, dt=1.2)
            res.opening_latency = round(res.events[-1]["t"] - res.events[-2]["t"], 1)
            continue

        intent = classify(line)
        if intent != "unparsed":
            unparsed_streak = 0

        if intent == "decline":
            agent_says(FALLBACK["decline"], heard=line)
            res.outcome = "declined"
            res.unanswered.append("Practice would not deal with an automated caller. Please ring them yourself.")
            break
        if intent == "identity":
            agent_says(FALLBACK["identity"], heard=line, unanswered=line)
            res.unanswered.append(f"Identity check requested: \"{line}\"")
        elif intent == "clinical":
            agent_says(FALLBACK["clinical"], heard=line, unanswered=line)
            res.unanswered.append(f"Clinical question asked: \"{line}\"")
        elif intent == "offer":
            pending_offer = parse_offer(line) or line
            agent_says(f"Yes, that works for her. So that is {pending_offer}. Can you confirm that?", heard=line)
        elif intent == "confirm" and pending_offer:
            res.agreed = pending_offer
            agent_says("Thank you. I will let her know. Goodbye.", heard=line, agreed=pending_offer)
            res.outcome = "booked"
            break
        elif intent == "unparsed":
            unparsed_streak += 1
            if unparsed_streak >= MAX_UNPARSED_TURNS:
                agent_says(FALLBACK["unparsed_limit"], heard=line)
                res.outcome = "unparsed_offer_callback"
                res.unanswered.append("Call could not be followed. Offered to connect or call back.")
                break
            agent_says(FALLBACK["unparsed"], heard=line)
        else:
            answer = answer_from_record(intent, rec)
            if answer is None:
                agent_says(FALLBACK["not_in_record"], heard=line, unanswered=line)
                res.unanswered.append(f"Not in her answers: \"{line}\"")
            else:
                agent_says(answer, heard=line)

    if res.outcome == "in_progress":
        res.outcome = "ended_without_booking"
    event("system", "hangup", f"Call ended: {res.outcome}", 1)
    append_audit(rid, "system", said=f"Call ended: {res.outcome}", agreed=res.agreed or "")
    return res
