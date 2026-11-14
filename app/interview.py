"""The bounded interview: a fixed question set, not open-ended chat.

Answers are tidied by a local model when Ollama is running, and by
plain heuristics when it is not. Nothing here ever calls a cloud API.
"""
from __future__ import annotations

import json
import os
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
# First one that is installed wins, so weaker devices can pull a smaller model.
OLLAMA_MODELS = [m for m in [
    os.environ.get("GP_AGENT_LLM", ""),
    "qwen2.5:3b-instruct", "qwen2.5:3b", "llama3.2:3b",
    "qwen2.5:1.5b-instruct", "qwen2.5:1.5b", "llama3.2:1b", "qwen2.5:0.5b-instruct",
] if m]

# Number held on this device from setup. Never spoken on the call.
PATIENT_NUMBER = "07700 900 123"


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    # (label shown, value stored in the record)
    options: tuple[tuple[str, str], ...] = ()
    multi: bool = False
    hint: str = ""
    # (label of the "other" option, label of the text box it reveals); None = no free text
    other: Optional[tuple[str, str]] = ("Something else", "Tell us more")
    short: str = ""  # label used on the check-your-answers page


QUESTIONS: list[Question] = [
    Question("reason", "What would you like to see a doctor about?",
             hint="Describe it in your own words. For example, “my knee has been hurting when I go up stairs”.",
             other=None, short="What it's about"),
    Question("duration", "How long has it been going on?", (
        ("A few days", "a few days"), ("About a week", "about a week"),
        ("A few weeks", "a few weeks"), ("Months or longer", "months or longer"),
    ), other=("Something else", "How long?"), short="How long"),
    Question("trajectory", "Is it getting better, worse, or staying the same?", (
        ("Getting worse", "worse"), ("Staying the same", "same"), ("Getting better", "better"),
    ), other=None, short="Better or worse"),
    Question("seen_before", "Have you seen anyone about this before?", (
        ("No, this is new", "no"), ("Yes, in the last few weeks", "yes, in the last few weeks"),
        ("Yes, longer ago", "yes, longer ago"),
    ), other=None, short="Seen before"),
    Question("taking_anything", "Are you taking anything for it at the moment?", (
        ("No", "nothing"), ("Paracetamol", "paracetamol"), ("Ibuprofen", "ibuprofen"),
    ), hint="Include medicines, creams or anything else you are using.",
       other=("Something else", "What are you taking?"), short="Taking for it"),
    Question("urgency", "How soon do you need to be seen?",  (
        ("Today if possible", "today if possible"), ("This week", "this week"),
        ("In the next 2 weeks", "in the next couple of weeks"),
    ), hint="If you need help right now, call 999 or 111 instead.", other=None, short="How soon"),
    Question("appointment_type", "How would you like to speak to someone?", (
        ("Come in to the surgery", "face to face"), ("Phone call", "a phone call"),
        ("Either is fine", "either face to face or a phone call"),
    ), other=None, short="Type of appointment"),
    Question("times", "Which times suit you?", (
        ("Weekday mornings", "weekday mornings"), ("Weekday afternoons", "weekday afternoons"),
        ("After 5pm", "after 5pm"), ("Any time", "any time"),
    ), multi=True, hint="Select all that apply.", other=("Something else", "Tell us which times"), short="Times that suit"),
    Question("preferred_clinician", "Would you like to see a particular doctor or nurse?", (
        ("No preference", "no"),
    ), other=("Yes", "Name of doctor or nurse"), short="Doctor or nurse"),
    Question("access_needs", "Is there anything that would help you at your appointment?", (
        ("Speak slowly and clearly", "speak slowly and clearly"), ("An interpreter", "an interpreter"),
        ("Step-free access", "step-free access"), ("Nothing needed", "no"),
    ), multi=True, hint="Select all that apply.", other=("Something else", "What would help?"), short="Help at appointment"),
    Question("contact_number", f"Is {PATIENT_NUMBER} the best number to contact you on?", (
        ("Yes", "yes"),
    ), hint="The surgery may need to call you back.", other=("No, use a different number", "Phone number"),
       short="Contact number"),
]
QUESTION_KEYS = [q.key for q in QUESTIONS]
QUESTION_BY_KEY = {q.key: q for q in QUESTIONS}
_SKIP_LABELS = {
    "duration": "how long it's been going on",
    "trajectory": "whether it's changing",
    "seen_before": "whether you've been seen before",
    "taking_anything": "what you're taking",
    "urgency": "how soon you need to be seen",
    "appointment_type": "how you'd like to be seen",
    "times": "which times suit you",
    "preferred_clinician": "who you'd like to see",
    "access_needs": "what would help at your appointment",
}

# How spoken answers map onto the options. First match wins for single-choice questions.
_SPOKEN: dict[str, list[tuple[str, str]]] = {
    "trajectory": [("Getting worse", r"worse"), ("Getting better", r"better|improv"),
                   ("Staying the same", r"same|no change|not changed|steady")],
    "seen_before": [("Yes, longer ago", r"months|years?\b|long time|ages|while ago"),
                    ("Yes, in the last few weeks", r"recent|weeks?|days? ago|yesterday"),
                    ("No, this is new", r"^\W*(no|nope|never|not yet)\b|first time|\bnew\b")],
    "taking_anything": [("Paracetamol", r"paracetamol|calpol"), ("Ibuprofen", r"ibuprofen|nurofen"),
                        ("No", r"^\W*(no|nothing|none|not taking)\b")],
    "urgency": [("Today if possible", r"today|urgent|asap|as soon as|straight away"),
                ("This week", r"this week|few days|next week|week is fine"),
                ("In the next 2 weeks", r"no rush|not urgent|couple of weeks|two weeks|whenever")],
    "appointment_type": [("Either is fine", r"either|don'?t mind|whichever|\bboth\b|doesn'?t matter"),
                         ("Phone call", r"phone|call me|ring me|\bcall\b"),
                         ("Come in to the surgery", r"come in|in person|face to face|see (them|someone|a doctor)|visit")],
    "times": [("Weekday mornings", r"morning"), ("Weekday afternoons", r"afternoon"),
              ("After 5pm", r"evening|after (5|five|work)|late"), ("Any time", r"any ?time|whenever|don'?t mind|any day")],
    "preferred_clinician": [("No preference", r"^\W*(no|nope|don'?t mind|anyone|any)\b|no preference|doesn'?t matter")],
    "access_needs": [("Speak slowly and clearly", r"slow|hearing|deaf|clear"), ("An interpreter", r"interpret|translat"),
                     ("Step-free access", r"wheelchair|\bsteps?\b|stairs|ramp|walking|frame"),
                     ("Nothing needed", r"^\W*(no|nothing|none|i'?m fine)\b|nothing needed")],
    "contact_number": [("Yes", r"^\W*(yes|yeah|yep|correct|that'?s (right|fine|correct|it))")],
}


def match_spoken(q: Question, text: str) -> Optional[str]:
    """Map a spoken answer onto the question's options, or None to keep it as said."""
    if q.key == "contact_number" and len(re.sub(r"\D", "", text)) >= 10:
        return None
    hits = [label for label, pattern in _SPOKEN.get(q.key, []) if re.search(pattern, text, re.I)]
    if not hits:
        return None
    if not q.multi:
        return hits[0]
    exclusive = {"Nothing needed", "Any time"}
    real = [h for h in hits if h not in exclusive]
    return ", ".join(real) if real else hits[0]


def normalise(q: Question, text: str) -> str:
    """Map tapped option labels to their record value; leave free text alone."""
    by_label = {label.lower(): value for label, value in q.options}
    parts = [p.strip() for p in re.split(r",|;", text)] if q.multi else [text.strip()]
    mapped = [by_label.get(p.lower(), p) for p in parts if p]
    return ", ".join(mapped)


def _split_list(text: str) -> list[str]:
    return [p.strip() for p in re.split(r",|;", text or "") if p.strip()]

INTERVIEW_PROMPT = """You are helping a patient prepare a GP appointment request.
You do not give medical advice. You do not assess how serious anything is.
You do not suggest what the problem might be.

From the patient's message below, extract ONLY facts she states explicitly.
Return JSON with these keys, using null for anything not stated:
duration, trajectory (better|worse|same), seen_before, taking_anything,
urgency, appointment_type (face to face|a phone call), times,
preferred_clinician, access_needs, reason_for_contact
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
_ollama_model: Optional[str] = None


def ollama_model() -> Optional[str]:
    """The installed local model to use, checked once. None if Ollama isn't running."""
    global _ollama_up, _ollama_model
    if _ollama_up is None:
        try:
            resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=1)
            resp.raise_for_status()
            installed = {m["name"] for m in resp.json().get("models", [])}
            installed |= {n.removesuffix(":latest") for n in installed}
            _ollama_model = next((m for m in OLLAMA_MODELS if m in installed), None)
        except (requests.RequestException, ValueError, KeyError):
            _ollama_model = None
        _ollama_up = _ollama_model is not None
    return _ollama_model


def ollama_available() -> bool:
    return ollama_model() is not None


def _ollama_extract(text: str) -> dict:
    """Local-model extraction. Returns {} if no local model is installed."""
    model = ollama_model()
    if not model:
        return {}
    try:
        resp = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": model, "prompt": INTERVIEW_PROMPT + text,
                  "stream": False, "format": "json", "options": {"temperature": 0}},
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
        found["appointment_type"] = "a phone call"
    # Preferences only count when stated as preferences, not as symptoms
    # ("it hurts in the morning" is not "mornings suit me").
    if re.search(r"as soon as possible|asap|(seen|appointment|see (someone|a doctor)) today|urgent(ly)? (appointment|please)", t):
        found["urgency"] = "today if possible"
    elif re.search(r"(seen|appointment|sometime|some time) this week|this week is fine", t):
        found["urgency"] = "this week"
    pref = re.search(r"(prefer|best|suits?|free|available|can (do|come)|good for me|works? for me)[^.]{0,40}", t)
    pref2 = re.search(r"(mornings?|afternoons?|evenings?)[^.]{0,15}(are|is|would be) (best|good|better|fine|easier)", t)
    window = (pref.group(0) if pref else "") + " " + (pref2.group(0) if pref2 else "")
    times = [v for word, v in (("morning", "weekday mornings"), ("afternoon", "weekday afternoons"),
                               ("evening", "after 5pm"), ("after work", "after 5pm")) if word in window]
    if times:
        found["times"] = ", ".join(dict.fromkeys(times))
    m = re.search(r"\b(?:see|with) (?:dr\.?|doctor) ([A-Z][a-z]+)", text)
    if m:
        found["preferred_clinician"] = f"Dr {m.group(1)}"
    needs = [v for pattern, v in ((r"hard of hearing|\bdeaf\b|hearing aid", "speak slowly and clearly"),
                                  (r"interpreter|translator", "an interpreter"),
                                  (r"wheelchair|walking frame|zimmer|mobility scooter", "step-free access")) if re.search(pattern, t)]
    if needs:
        found["access_needs"] = ", ".join(needs)
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
    raw: dict[str, str] = field(default_factory=dict)
    history: list[tuple[str, list[str]]] = field(default_factory=list)

    def next_question(self) -> Optional[Question]:
        for q in QUESTIONS:
            if q.key not in self.answers:
                return q
        return None

    def _ask(self, q: Question, ack: str = "") -> dict:
        self.transcript.append({"role": "assistant", "text": f"{ack} {q.text}".strip()})
        return {
            "type": "question",
            "key": q.key,
            "ack": ack,
            "text": q.text,
            "hint": q.hint,
            "options": [label for label, _ in q.options],
            "multi": q.multi,
            "other": list(q.other) if q.other else None,
            "value": self.raw.get(q.key, ""),
            "can_go_back": bool(self.history),
            "progress": {"done": len(self.answers), "total": len(QUESTIONS),
                         "number": QUESTION_KEYS.index(q.key) + 1},
        }

    def start(self) -> dict:
        return self._ask(QUESTIONS[0])

    def go_back(self) -> dict:
        """Undo the last answer (and anything inferred from it) and re-ask it."""
        if self.crisis:
            raise ValueError("Interview stopped")
        self.finished = False
        if not self.history:
            return self._ask(QUESTIONS[0])
        key, inferred = self.history.pop()
        for k in [key, *inferred]:
            self.answers.pop(k, None)
            self.inferred.discard(k)
            if k != key:
                self.raw.pop(k, None)
        return self._ask(QUESTION_BY_KEY[key])

    def change(self, key: str) -> dict:
        """Re-ask one question from the check-your-answers page."""
        if self.crisis or key not in QUESTION_BY_KEY:
            raise ValueError("Cannot change that")
        self.answers.pop(key, None)
        self.inferred.discard(key)
        self.finished = False
        return self._ask(QUESTION_BY_KEY[key])

    def summary_rows(self) -> list[dict]:
        """Rows for the check-your-answers page, in question order."""
        def shown(q: Question) -> str:
            value = self.raw.get(q.key) or self.answers.get(q.key, "")
            if q.key == "contact_number" and value.lower() == "yes":
                return PATIENT_NUMBER
            return value

        return [{"key": q.key, "label": q.short or q.text, "value": shown(q),
                 "inferred": q.key in self.inferred} for q in QUESTIONS if q.key in self.answers]

    def submit_answer(self, text: str, spoken: bool = False) -> dict:
        text = text.strip()
        if not text:
            q = self.next_question()
            return self._ask(q)

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

        ack = ""
        if asks_for_opinion(text):
            ack = OPINION_MESSAGE
            kept = [s for s in re.split(r"(?<=[.?!])\s+|\s+and (?=is|could|should|what)", text) if s and not asks_for_opinion(s)]
            text = " ".join(kept).strip()
            if len(text.split()) < 2:
                return self._ask(current, ack)

        key = current.key
        heard = text
        if spoken and current.options:
            text = match_spoken(current, text) or text
        self.answers[key] = normalise(current, text)
        self.raw[key] = text
        if key == "reason":
            self.reason_phrase = reason_for_contact(text)

        newly_inferred = []
        tapped = {label.lower() for label, _ in current.options}
        if not all(p.strip().lower() in tapped for p in text.split(",")):
            extracted = _heuristic_extract(text)
            for k, v in _ollama_extract(text).items():
                if k == "reason_for_contact" and key == "reason":
                    self.reason_phrase = v
                extracted.setdefault(k, v)
            for k, v in extracted.items():
                if k in QUESTION_KEYS and k not in self.answers:
                    self.answers[k] = v
                    self.inferred.add(k)
                    newly_inferred.append(k)
        self.history.append((key, newly_inferred))
        if newly_inferred:
            things = [_SKIP_LABELS.get(k, k) for k in QUESTION_KEYS if k in newly_inferred]
            joined = things[0] if len(things) == 1 else ", ".join(things[:-1]) + " and " + things[-1]
            ack = f"{ack} You've already told us {joined}, so we've skipped {'that question' if len(things) == 1 else 'those questions'}. You can change {'it' if len(things) == 1 else 'them'} at the end.".strip()

        nxt = self.next_question()
        if nxt is None:
            self.finished = True
            closing = "Thank you, that's everything. Please check what I've written down and fix anything that's wrong."
            self.transcript.append({"role": "assistant", "text": closing})
            return {"type": "review", "text": closing, "record": self.build_record().to_dict(),
                    "progress": {"done": len(QUESTIONS), "total": len(QUESTIONS)},
                    "heard": heard if spoken else None}
        result = self._ask(nxt, ack)
        result["heard"] = heard if spoken else None
        result["understood"] = text if spoken and text != heard else None
        return result

    def build_record(self) -> Record:
        a = self.answers
        seen = a.get("seen_before", "")
        prefs = Preferences(
            urgency=a.get("urgency", ""),
            appointment_type=a.get("appointment_type", ""),
            times=_split_list(a.get("times", "")),
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
            access_needs=[n for n in _split_list(a.get("access_needs", "")) if not is_negative(n)],
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
        parts.append(f"ideally {join_or(prefs.times)}")
    sentence = ", ".join(parts) + "."
    return f"{sentence} It is about {with_article(reason)}."


_NO_ARTICLE = {"mental health", "women's health", "test results"}


def with_article(reason: str) -> str:
    if reason.startswith(("a ", "an ")) or reason in _NO_ARTICLE:
        return reason
    return f"a {reason}"


def join_or(items: list[str]) -> str:
    items = [i.strip().rstrip(".") for i in items if i.strip()]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]
