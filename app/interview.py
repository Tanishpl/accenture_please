"""The bounded interview: a fixed question set, not open-ended chat.

Answers are tidied by a local model when Ollama is running, and by
plain heuristics when it is not. Nothing here ever calls a cloud API.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

from app.record import ClinicalDetail, Preferences, Record
from app.safety import (
    DISTRESS_MESSAGE,
    OPINION_MESSAGE,
    RED_FLAG_MESSAGE,
    asks_for_opinion,
    check_distress,
    check_red_flags,
)

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b-instruct"

QUESTIONS: list[tuple[str, str]] = [
    ("reason", "What would you like to see the doctor about?"),
    ("duration", "How long has it been going on?"),
    ("trajectory", "Has it been getting better, worse, or staying the same?"),
    ("seen_before", "Have you been seen about this before, and roughly when?"),
    ("taking_anything", "Are you taking anything for it at the moment?"),
    ("urgency", "Do you need to be seen urgently, or is some time this week alright?"),
    ("appointment_type", "Would you prefer to come in, or a phone call?"),
    ("times", "Which days and times suit you best?"),
    ("preferred_clinician", "Is there a particular doctor or nurse you would rather see?"),
    ("access_needs", "Is there anything that would help on the day, like an interpreter, step-free access, or being spoken to slowly?"),
    ("contact_number", "Is this still the best number to reach you on?"),
]
QUESTION_KEYS = [k for k, _ in QUESTIONS]

INTERVIEW_PROMPT = """You are helping a patient prepare a GP appointment request.
You do not give medical advice. You do not assess how serious anything is.
You do not suggest what the problem might be.

From the patient's message below, extract ONLY facts she states explicitly.
Return JSON with these keys, using null for anything not stated:
duration, trajectory (better|worse|same), taking_anything, urgency,
appointment_type (face to face|phone), times, reason_for_contact
(a 2-4 word non-clinical phrase naming the general area, e.g. "knee problem").
Never guess. Return JSON only.

Patient message: """

_BODY_AREAS = {
    "knee": "knee problem", "back": "back problem", "shoulder": "shoulder problem",
    "hip": "hip problem", "foot": "foot problem", "feet": "foot problem",
    "ankle": "ankle problem", "hand": "hand problem", "wrist": "wrist problem",
    "neck": "neck problem", "skin": "skin problem", "rash": "skin problem",
    "mole": "skin problem", "ear": "ear problem", "eye": "eye problem",
    "throat": "throat problem", "cough": "cough", "stomach": "stomach problem",
    "tummy": "stomach problem", "bowel": "bowel problem", "period": "women's health",
    "sleep": "sleep problem", "mood": "mental health", "anxiety": "mental health",
    "medication": "medication review", "prescription": "medication review",
    "blood test": "test results", "results": "test results",
}

_NEGATIVE = re.compile(r"^\s*(no|nope|none|nothing|not really|n/a|no thanks)\b", re.I)


def is_negative(text: str) -> bool:
    return bool(_NEGATIVE.match(text or ""))


_ollama_up: Optional[bool] = None


def ollama_available() -> bool:
    global _ollama_up
    if _ollama_up is None:
        try:
            requests.get(f"{OLLAMA_BASE}/api/tags", timeout=1).raise_for_status()
            _ollama_up = True
        except requests.RequestException:
            _ollama_up = False
    return _ollama_up


def _ollama_extract(text: str) -> dict:
    """Local-model extraction. Returns {} if Ollama is not running."""
    if not ollama_available():
        return {}
    try:
        resp = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": INTERVIEW_PROMPT + text,
                  "stream": False, "format": "json"},
            timeout=60,
        )
        resp.raise_for_status()
        data = json.loads(resp.json().get("response", "{}"))
        return {k: v for k, v in data.items() if isinstance(v, str) and v.strip()}
    except (requests.RequestException, ValueError):
        return {}


def _heuristic_extract(text: str) -> dict:
    found: dict[str, str] = {}
    t = text.lower()
    m = re.search(r"\b(\d+|a|an|one|two|three|four|five|six|few|couple of)\s+(day|week|month|year)s?\b", t)
    if m:
        found["duration"] = m.group(0)
    elif re.search(r"\bsince (yesterday|last week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t):
        found["duration"] = re.search(r"since \w+( week)?", t).group(0)
    # Only a stated trend counts; "worse going up stairs" is not a trajectory.
    if re.search(r"(getting|got|been getting|growing) worse|worsening|worse and worse", t):
        found["trajectory"] = "worse"
    elif re.search(r"(getting|got) better|improving", t):
        found["trajectory"] = "better"
    elif re.search(r"(about|staying) the same|no change", t):
        found["trajectory"] = "same"
    m = re.search(r"\b(taking|took|tried|on) (paracetamol|ibuprofen|codeine|antihistamines?|[a-z]+ cream)\b", t)
    if m:
        found["taking_anything"] = m.group(2)
    if re.search(r"face to face|in person|come in", t):
        found["appointment_type"] = "face to face"
    elif re.search(r"phone call|by phone|over the phone|ring me", t):
        found["appointment_type"] = "phone"
    return found


def reason_for_contact(text: str) -> str:
    """A short non-clinical phrase for care navigation, never the description."""
    t = text.lower()
    for word, phrase in _BODY_AREAS.items():
        if re.search(rf"\b{re.escape(word)}", t):
            return phrase
    return "a general health matter"


@dataclass
class InterviewSession:
    session_id: str
    answers: dict[str, str] = field(default_factory=dict)
    inferred: set[str] = field(default_factory=set)
    transcript: list[dict] = field(default_factory=list)
    finished: bool = False
    crisis: Optional[dict] = None
    reason_phrase: str = ""

    def next_question(self) -> Optional[tuple[str, str]]:
        for key, text in QUESTIONS:
            if key not in self.answers:
                return key, text
        return None

    def start(self) -> dict:
        key, text = QUESTIONS[0]
        self._say(text)
        return {"type": "question", "key": key, "text": text}

    def _say(self, text: str) -> None:
        self.transcript.append({"role": "assistant", "text": text})

    def submit_answer(self, text: str) -> dict:
        text = text.strip()
        if not text:
            q = self.next_question()
            return {"type": "question", "key": q[0], "text": q[1]}

        if check_distress(text):
            # Not stored, not summarised: the whole session is dropped.
            self.answers.clear()
            self.transcript.clear()
            self.finished = True
            self.crisis = {"kind": "distress"}
            return {"type": "crisis", "kind": "distress", "message": DISTRESS_MESSAGE}

        flags = check_red_flags(text)
        if flags:
            self.transcript.append({"role": "patient", "text": text})
            self.finished = True
            self.crisis = {"kind": "red_flag", "flags": flags}
            return {"type": "crisis", "kind": "red_flag", "flags": flags, "message": RED_FLAG_MESSAGE}

        self.transcript.append({"role": "patient", "text": text})
        current = self.next_question()

        prefix = ""
        if asks_for_opinion(text):
            prefix = OPINION_MESSAGE + " "
            kept = [s for s in re.split(r"(?<=[.?!])\s+|\s+and (?=is|could|should|what)", text) if s and not asks_for_opinion(s)]
            text = " ".join(kept).strip()
            if len(text.split()) < 2:
                reply = prefix + current[1]
                self._say(reply)
                return {"type": "question", "key": current[0], "text": reply}

        key = current[0]
        self.answers[key] = text
        if key == "reason":
            self.reason_phrase = reason_for_contact(text)

        extracted = _heuristic_extract(text)
        for k, v in _ollama_extract(text).items():
            if k == "reason_for_contact" and key == "reason":
                self.reason_phrase = v
            extracted.setdefault(k, v)
        for k, v in extracted.items():
            if k in QUESTION_KEYS and k not in self.answers:
                self.answers[k] = v
                self.inferred.add(k)

        nxt = self.next_question()
        if nxt is None:
            self.finished = True
            closing = "Thank you. Here is what I have. Please check it and correct anything that is wrong."
            self._say(closing)
            return {"type": "review", "text": closing, "record": self.build_record().to_dict()}
        self._say(prefix + nxt[1])
        return {"type": "question", "key": nxt[0], "text": prefix + nxt[1]}

    def build_record(self) -> Record:
        a = self.answers
        seen = a.get("seen_before", "")
        prefs = Preferences(
            urgency=a.get("urgency", ""),
            appointment_type=a.get("appointment_type", ""),
            times=[a["times"]] if a.get("times") else [],
            preferred_clinician=None if is_negative(a.get("preferred_clinician", "no")) else a["preferred_clinician"],
        )
        reason = self.reason_phrase or reason_for_contact(a.get("reason", ""))
        return Record(
            patient_ref=f"local-{self.session_id[:8]}",
            reason_for_contact=reason,
            clinical_detail=ClinicalDetail(
                description=a.get("reason", ""),
                duration=a.get("duration", ""),
                trajectory=a.get("trajectory", ""),
                seen_before_for_this=bool(seen) and not is_negative(seen),
                taking_anything="" if is_negative(a.get("taking_anything", "")) else a.get("taking_anything", ""),
            ),
            preferences=prefs,
            access_needs=[] if is_negative(a.get("access_needs", "no")) else [a["access_needs"]],
            red_flags=[],
            spoken_summary=build_spoken_summary(reason, prefs),
            confirmed_by_patient=False,
        )


def build_spoken_summary(reason: str, prefs: Preferences) -> str:
    """Deterministic template, deliberately not model-generated.

    Built only from the non-clinical reason phrase and preferences, so it
    cannot contain symptoms, diagnosis or identifying details no matter
    what the local model does.
    """
    urgency = prefs.urgency.strip().rstrip(".") if prefs.urgency else "soon"
    parts = [f"A registered patient would like an appointment, {urgency}"]
    if prefs.times:
        parts.append(f"ideally {', '.join(t.strip().rstrip('.') for t in prefs.times)}")
    sentence = ", ".join(parts) + "."
    needs_article = not (reason.startswith(("a ", "an ")) or reason in _NO_ARTICLE)
    return f"{sentence} It is about {'a ' if needs_article else ''}{reason}."


_NO_ARTICLE = {"mental health", "women's health", "test results"}
