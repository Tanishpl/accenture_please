"""Agent turn logic for the call, and the fallback table.

CallBrain holds one conversation with the practice. The simulated call
(run_call, driven by mock/receptionist.py) and a live phone call both feed
it the receptionist's words one turn at a time. Every line the agent
speaks is either a fixed script line or built from an allowlisted record
field via speakable_text(), so clinical detail cannot reach the call even
by accident.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import delegation
from app.interview import join_or
from app.record import Record, _connect, append_audit
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
# A live demo call can't patch the patient in, so it mustn't promise to.
LIVE_OPENING_LINE = (
    "Hello, this is an automated assistant calling on behalf of a registered "
    "patient. She would like to book an appointment. I have her answers ready, "
    "and if you need to speak to her, she can call you back."
)

FALLBACK = {
    "not_in_record": "I don't have that answer. I can ask her and ring you back, or she can call or email the surgery directly.",
    "clinical": "I can't answer that, I'm not clinical. She can speak to a clinician by calling the surgery, or I can pass on that she'd like a call back.",
    "identity": "I can't confirm her identity, I'm an assistant. She can confirm that herself if she calls or comes in.",
    "decline": "Understood, I'll ask her to call you directly. Thank you.",
    "unparsed_limit": "Sorry, I'm having trouble following. I can ring you back later, or she can call or email the surgery directly. Thank you.",
    "unparsed": "Sorry, I didn't catch that. Could you say it again?",
    "who": "I'm an automated assistant calling on behalf of a registered patient at your surgery. I can't share her details, but I can book an appointment for her.",
    "speak_to_patient": "She isn't on this call, so I can't put her through right now. She can call you back, or email the surgery, if you need to speak to her.",
    "close": "Thank you for your help. I'll let her know. Goodbye.",
    "turn_limit": "I'll let her know and she can call you back. Thank you, goodbye.",
    "silence": "Sorry, I didn't hear anything. Are you still there?",
    "silence_end": "I'll let her know and she can call you back. Thank you, goodbye.",
    "booked": "Thank you. I'll let her know. Goodbye.",
}

_INTENTS: list[tuple[str, re.Pattern]] = [
    ("decline", re.compile(
        r"(don'?t|do not|can'?t|cannot|won'?t) (take|accept|deal with|speak to|talk to|book with).{0,30}(automated|ai\b|robots?|machines?|bots?|computers?)"
        r"|(automated|ai\b|robot|machine|bot).{0,40}(ring|call) (herself|themselves)"
        r"|not (talking|speaking) to an? (robot|machine|computer|bot)"
        r"|she('ll| will) (need|have) to (ring|call)( us)? (herself|in person)", re.I)),
    ("who", re.compile(
        r"who('?s| is) (this|that|calling|speaking|on the line)|who are you|who am i speaking to"
        r"|are you (a |an )?(robot|bot|machine|real person|real|human|ai\b|computer|recording)"
        r"|is this (a |an )?(ai\b|robot|bot|automated|recording|computer|machine)", re.I)),
    ("speak_to_patient", re.compile(
        r"(speak|talk) (to|with) (her|the patient|herself|the lady)|put her (on|through)"
        r"|is she (there|with you|available|on the line)|can she come to the phone", re.I)),
    ("identity", re.compile(r"date of birth|\bdob\b|(?<!email )address|postcode|nhs number|confirm (her|their) (identity|details)|security question", re.I)),
    ("clinical", re.compile(r"sound like|should (she|he|they) be seen|needs? to be seen|how (bad|serious|severe)|symptoms|pain (score|level)|is it serious|red flag", re.I)),
    ("confirm", re.compile(r"\b(booked|confirmed|that'?s right|all done|see her then)\b", re.I)),
    ("offer", re.compile(r"$^", re.I)),  # placeholder: offers are detected by find_time()/_OFFER_DAY below
    ("reason", re.compile(
        r"how can i help|what can i do for you|how can we help|what('?s| is) it (about|for|regarding|in connection with)"
        r"|whats it (about|for|regarding)|what('?s| is) (this|the call) (about|regarding)|reason for|what does she need"
        r"|what('?s| is) the (problem|issue)", re.I)),
    ("urgency", re.compile(r"how (soon|urgent)|when (does|would) she (want|like|need) to be seen|is it urgent", re.I)),
    ("appointment_type", re.compile(r"come in|face to face|in person|phone (call|appointment)|telephone", re.I)),
    ("times", re.compile(r"when suits|what (days|times)|availab|when (is|would be) (good|best)|when can she|what time", re.I)),
    ("clinician", re.compile(r"particular (doctor|gp|nurse|clinician)|which (doctor|gp|clinician)|preference for (a )?(doctor|gp)", re.I)),
]
_QUESTION_LIKE = re.compile(r"\?\s*$|^(what|can|could|does|do|is|are|has|have|will|would|when|where|who|how)\b", re.I)
_FILLER = re.compile(r"\b(um+|uh+|er+|erm|mm+hm|hmm+|hang on|bear with|hold on|one sec)\b|\.\.\.|…", re.I)
_YES = re.compile(r"^\W*(yes|yeah|yep|yup|correct|that'?s (right|fine|correct|it|great|perfect)|perfect|great|lovely|sure|ok(ay)?|fine)\b", re.I)
_NO = re.compile(r"^\W*(no|nope|sorry no|that doesn'?t work|that won'?t work)\b", re.I)
_CLOSE = re.compile(r"^\W*(ok(ay)?\W+)?(thanks|thank you|cheers|bye|goodbye|that'?s (all|everything|it))\b|\b(good ?bye|bye bye|bye now)\b", re.I)
_OFFER_DAY = re.compile(r"\b(i'?ve got|i have|i can (do|offer)|we (have|can do)|there'?s|how about|what about)\b.{0,30}\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)\b", re.I)
_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b", re.I)
_WITH = re.compile(r"\bwith (?:(dr\.?|doctor|nurse) (\w+)|the (\w+(?: \w+)?))", re.I)

_HOUR_WORDS = {w: i for i, w in enumerate(
    "one two three four five six seven eight nine ten eleven twelve".split(), start=1)}
_MIN_WORDS = {"oh five": 5, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25,
              "thirty": 30, "thirty five": 35, "forty": 40, "forty five": 45, "fifty": 50, "fifty five": 55}
_H = r"(\d{1,2}|" + "|".join(_HOUR_WORDS) + r")"
_M = "(" + "|".join(sorted(_MIN_WORDS, key=len, reverse=True)) + ")"
_AMPM = r"(am|pm|a\.m\.|p\.m\.)"


def _hour(token: str) -> int:
    return int(token) if token.isdigit() else _HOUR_WORDS[token.lower()]


def _ampm(token: str) -> str:
    return token.lower().replace(".", "")


_TIME_RULES: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"\b(\d{1,2})[:.](\d{2})\s?" + _AMPM + r"?(?=\W|$)", re.I),
     lambda m: f"{int(m.group(1))}:{m.group(2)}" + (_ampm(m.group(3)) if m.group(3) else "")),
    (re.compile(r"\bhalf (?:past )?" + _H + r"\b", re.I), lambda m: f"{_hour(m.group(1))}:30"),
    (re.compile(r"\bquarter past " + _H + r"\b", re.I), lambda m: f"{_hour(m.group(1))}:15"),
    (re.compile(r"\bquarter to " + _H + r"\b", re.I), lambda m: f"{(_hour(m.group(1)) - 2) % 12 + 1}:45"),
    (re.compile(r"\b" + _H + r" o'?clock\b", re.I), lambda m: f"{_hour(m.group(1))}:00"),
    (re.compile(r"\b" + _H + r"\s?" + _AMPM + r"(?=\W|$)", re.I), lambda m: f"{_hour(m.group(1))}{_ampm(m.group(2))}"),
    (re.compile(r"\b" + _H + r" " + _M + r"\b", re.I), lambda m: f"{_hour(m.group(1))}:{_MIN_WORDS[m.group(2).lower()]:02d}"),
    (re.compile(r"\bat " + _H + r"\b(?! (point|of|time|stage|day|week|month|year|minute|hour))", re.I),
     lambda m: f"{_hour(m.group(1))}:00"),
]


def find_time(utterance: str) -> Optional[str]:
    """First clock time in the utterance, written readably ("half nine" -> "9:30")."""
    found = []
    for rank, (pattern, fmt) in enumerate(_TIME_RULES):
        m = pattern.search(utterance)
        if m:
            try:
                # Position of the hour itself, so "at 9:20" prefers the more specific rule.
                found.append((m.start(1), rank, fmt(m)))
            except (KeyError, ValueError):
                continue
    return min(found)[2] if found else None


class CallRefused(Exception):
    pass


def classify(utterance: str) -> str:
    for intent, pattern in _INTENTS:
        if intent == "offer":
            if find_time(utterance) or _OFFER_DAY.search(utterance):
                return "offer"
            continue
        if pattern.search(utterance):
            return intent
    s = utterance.strip()
    if _QUESTION_LIKE.search(s):
        return "unknown_question"
    if len(re.findall(r"[a-z0-9']+", s.lower())) < 4 or _FILLER.search(s):
        return "unparsed"
    return "unknown_question"


def parse_offer(utterance: str) -> str:
    bits = []
    if (d := _DAY.search(utterance)):
        bits.append(d.group(0).capitalize())
    if (t := find_time(utterance)):
        bits.append(f"at {t}")
    if (w := _WITH.search(utterance)):
        if w.group(1):
            title = "Dr" if w.group(1).lower().startswith("d") else "Nurse"
            bits.append(f"with {title} {w.group(2).capitalize()}")
        else:
            bits.append(f"with the {w.group(3)}")
    return " ".join(bits)


def answer_from_record(intent: str, record: dict) -> Optional[str]:
    """Build an answer only from speakable fields. None means 'not in record'."""
    prefs = record["preferences"]
    if intent == "reason":
        return speakable_text(record, "spoken_summary")
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


def preflight(record: Record, credential: Optional[dict]) -> None:
    """Every check that must pass before any call, simulated or live."""
    if not record.confirmed_by_patient:
        raise CallRefused("The patient has not confirmed the record.")
    if not credential or delegation.mock_practice_endpoint(credential)["status"] != "authorised":
        raise CallRefused("No valid delegation credential. The patient may have revoked it.")
    if record.red_flags:
        raise CallRefused("Red flags were detected. This must not be booked as a routine call.")
    if attempts_today(record.request_id) >= MAX_ATTEMPTS_PER_DAY:
        raise CallRefused(f"Daily limit of {MAX_ATTEMPTS_PER_DAY} attempts reached for this request.")


class CallBrain:
    """One conversation with the practice, fed one receptionist turn at a time."""

    def __init__(self, record: Record, max_turns: int = 8, live: bool = False):
        self.live = live
        self.record = record.to_dict()
        self.request_id = record.request_id
        self.max_turns = max_turns
        self.outcome = "in_progress"
        self.agreed: Optional[str] = None
        self.unanswered: list[str] = []
        self.spoken_out: list[str] = []
        self.turns = 0
        self._pending_offer: Optional[str] = None
        self._unparsed_streak = 0
        self._silences = 0
        self._finished = False

    def _say(self, text: str, heard: str = "", unanswered: str = "", agreed: str = "") -> str:
        self.spoken_out.append(text)
        append_audit(self.request_id, "agent", said=text, heard=heard, unanswered=unanswered, agreed=agreed)
        return text

    def _end(self, outcome: str) -> None:
        if self.outcome == "in_progress":
            self.outcome = outcome

    def opening(self, heard: str = "") -> str:
        return self._say(LIVE_OPENING_LINE if self.live else OPENING_LINE, heard=heard)

    def respond(self, utterance: str) -> tuple[str, bool]:
        utterance = (utterance or "").strip()
        if not utterance:
            return self.silence()
        self._silences = 0
        self.turns += 1
        reply, end = self._turn(utterance)
        if not end and self.turns >= self.max_turns:
            self._say(FALLBACK["turn_limit"], heard=utterance)
            self._end("turn_limit")
            return FALLBACK["turn_limit"], True
        return reply, end

    def _turn(self, line: str) -> tuple[str, bool]:
        intent = classify(line)
        if intent != "unparsed":
            self._unparsed_streak = 0

        if intent == "decline":
            self.unanswered.append("Practice would not deal with an automated caller. Please ring them yourself.")
            self._end("declined")
            return self._say(FALLBACK["decline"], heard=line), True
        if intent == "who":
            return self._say(FALLBACK["who"], heard=line), False
        if intent == "speak_to_patient":
            self.unanswered.append(f"Asked to speak to her: \"{line}\"")
            return self._say(FALLBACK["speak_to_patient"], heard=line, unanswered=line), False
        if intent == "identity":
            self.unanswered.append(f"Identity check requested: \"{line}\"")
            return self._say(FALLBACK["identity"], heard=line, unanswered=line), False
        if intent == "clinical":
            self.unanswered.append(f"Clinical question asked: \"{line}\"")
            return self._say(FALLBACK["clinical"], heard=line, unanswered=line), False
        if intent == "offer":
            self._pending_offer = parse_offer(line) or line
            return self._say(f"Yes, that works for her. So that's {self._pending_offer}. Can you confirm that?", heard=line), False
        if intent == "confirm":
            if not self._pending_offer and find_time(line):
                self._pending_offer = parse_offer(line)
            if self._pending_offer:
                return self._book(line)
            return self._say("Thank you. Do you have an appointment you can offer her?", heard=line), False

        if self._pending_offer:
            if _YES.search(line):
                return self._book(line)
            if _NO.search(line):
                self._pending_offer = None
                times = answer_from_record("times", self.record)
                return self._say("Not to worry. What else do you have?" + (f" {times}" if times else ""), heard=line), False

        if intent in ("unparsed", "unknown_question") and _CLOSE.search(line):
            self._end("ended_without_booking")
            return self._say(FALLBACK["close"], heard=line), True

        if intent == "unparsed":
            self._unparsed_streak += 1
            if self._unparsed_streak >= MAX_UNPARSED_TURNS:
                self.unanswered.append("Call could not be followed. Offered to ring back.")
                self._end("unparsed_offer_callback")
                return self._say(FALLBACK["unparsed_limit"], heard=line), True
            return self._say(FALLBACK["unparsed"], heard=line), False

        answer = answer_from_record(intent, self.record)
        if answer is None:
            self.unanswered.append(f"Not in her answers: \"{line}\"")
            return self._say(FALLBACK["not_in_record"], heard=line, unanswered=line), False
        return self._say(answer, heard=line), False

    def _book(self, line: str) -> tuple[str, bool]:
        self.agreed = self._pending_offer
        self._pending_offer = None
        self._end("booked")
        return self._say(FALLBACK["booked"], heard=line, agreed=self.agreed), True

    def silence(self) -> tuple[str, bool]:
        self._silences += 1
        if self._silences >= 2:
            self._end("ended_without_booking")
            return self._say(FALLBACK["silence_end"], heard="[silence]"), True
        return self._say(FALLBACK["silence"], heard="[silence]"), False

    def finish(self, reason: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        self._end("ended_without_booking")
        said = f"Call ended: {self.outcome}" + (f" ({reason})" if reason else "")
        append_audit(self.request_id, "system", said=said, agreed=self.agreed or "")


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
    """Simulated call against the scripted receptionist."""
    preflight(record, credential)
    rid = record.request_id
    script = receptionist.script(scenario)

    res = CallResult(outcome="in_progress")
    clock = 0.0

    def event(speaker: str, kind: str, text: str, dt: float = 1.0) -> None:
        nonlocal clock
        clock += dt
        res.events.append({"t": round(clock, 1), "speaker": speaker, "kind": kind, "text": text})

    n = attempts_today(rid) + 1
    dial_note = f"Dial attempt {n} of {MAX_ATTEMPTS_PER_DAY} to {receptionist.PRACTICE_NUMBER} (ring at least {MIN_RING_SECONDS}s)"
    event("system", "dial", dial_note, 0)
    append_audit(rid, "system", said=dial_note)

    brain = CallBrain(record, max_turns=len(script) + 1)
    human_on_line = False

    for kind, line in script:
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
            event("agent", "speech", brain.opening(heard=line), 1.2)
            res.opening_latency = round(res.events[-1]["t"] - res.events[-2]["t"], 1)
            continue

        reply, end = brain.respond(line)
        event("agent", "speech", reply, 1.2)
        if end:
            break

    brain.finish()
    res.outcome = brain.outcome
    res.agreed = brain.agreed
    res.unanswered = brain.unanswered
    res.spoken_out = brain.spoken_out
    event("system", "hangup", f"Call ended: {res.outcome}", 1)
    return res
