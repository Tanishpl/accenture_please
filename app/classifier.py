"""Decide what we are hearing while waiting: IVR menu, recording, or a human.

Two layers:
1. Fast rules: catch the obvious recorded phrases ("you are number 5 in the
   queue") and anything we've already heard before. Recordings repeat; people
   don't.
2. Gemini: for everything else, especially "is this a live receptionist?" and
   "which key gets me to appointments?".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Literal

from google import genai
from pydantic import BaseModel, ValidationError

from .config import settings

log = logging.getLogger(__name__)
LLM_TIMEOUT_SECONDS = 2.5


class Heard(str, Enum):
    HOLD = "hold"            # music, silence, or a recorded queue message
    IVR_MENU = "ivr_menu"    # "press 1 for appointments..."
    HUMAN = "human"          # a live receptionist
    CLOSED = "closed"        # "the surgery is now closed" / "lines are full, call back"


VERDICT_REASONS = {
    Heard.HOLD: "waiting in the queue",
    Heard.IVR_MENU: "phone menu",
    Heard.HUMAN: "a person answered",
    Heard.CLOSED: "the surgery appears to be closed or its queue is full",
}


@dataclass
class Verdict:
    heard: Heard
    digits: str | None = None
    reason: str = field(init=False)

    def __post_init__(self) -> None:
        self.reason = VERDICT_REASONS[self.heard]


class GeminiVerdict(BaseModel):
    label: Literal["hold", "ivr_menu", "human", "closed"]


QUEUE_PATTERNS = [
    r"\byou are (?:caller )?(?:number|no\.?|position)\b",
    r"\bin the queue\b",
    r"\bplease (?:continue to )?hold\b",
    r"\bcalls? (?:are|is) (?:being )?(?:recorded|monitored)\b",
    r"\byour call is important\b",
    r"\bplease stay on the line\b",
    r"\bthank you for (?:your )?(?:patience|waiting|holding)\b",
]
CLOSED_PATTERNS = [
    r"\bsurgery is (?:now )?closed\b",
    r"\bour opening hours are\b.*\bplease call back\b",
    r"\bqueue is (?:currently )?full\b",
    r"\bunable to take your call\b.*\bcall back\b",
]
MENU_PATTERN = re.compile(r"\bpress (\d|star|hash)\b", re.I)


class HoldClassifier:
    def __init__(self, surgery_name: str, reason: str) -> None:
        self.surgery_name = surgery_name
        self.reason = reason
        self.heard_before: list[str] = []
        self._client = genai.Client(api_key=settings.gemini_api_key).aio if settings.gemini_api_key else None

    def _is_repeat(self, text: str) -> bool:
        return any(SequenceMatcher(None, text.lower(), old.lower()).ratio() > 0.8 for old in self.heard_before)

    def quick_rules(self, text: str) -> Verdict | None:
        t = text.lower()
        if any(re.search(p, t) for p in CLOSED_PATTERNS):
            return Verdict(Heard.CLOSED)
        if self._is_repeat(text):
            return Verdict(Heard.HOLD)
        if any(re.search(p, t) for p in QUEUE_PATTERNS) and not MENU_PATTERN.search(t):
            return Verdict(Heard.HOLD)
        return None

    async def classify(self, text: str) -> Verdict:
        text = text.strip()
        if not text:
            return Verdict(Heard.HOLD)
        verdict = self.quick_rules(text)
        if verdict is None:
            llm_verdict = await self._ask_llm(text) if self._client else None
            verdict = llm_verdict or self.keyword_rules(text)
        self.heard_before.append(text)
        return verdict

    def keyword_rules(self, text: str) -> Verdict:
        """No Gemini credentials: crude keyword guess (good enough for a demo)."""
        menu = _menu_digit_for_appointments(text)
        if menu:
            return Verdict(Heard.IVR_MENU, digits=menu)
        if re.search(r"\b(speaking|how can i help|how may i help|can i help)\b", text, re.I):
            return Verdict(Heard.HUMAN)
        return Verdict(Heard.HOLD)

    async def _ask_llm(self, text: str) -> Verdict | None:
        previous = "\n".join(f"- {t}" for t in self.heard_before[-8:]) or "(nothing yet)"
        prompt = (
            f"We are an automated assistant phoning {self.surgery_name}, a UK NHS GP surgery, "
            f"to book an appointment (reason: {self.reason}). We are waiting in the phone queue "
            "and hear a speech-to-text transcript of the line.\n\n"
            f"Earlier on this call we heard:\n{previous}\n\n"
            f"Latest audio transcript:\n\"{text}\"\n\n"
            "Classify the latest audio:\n"
            "- hold: recorded announcement, queue position, music lyrics, or noise\n"
            "- ivr_menu: automated menu asking to press keys. Set digits to the key(s) that "
            "lead to booking a GP appointment (or reception, if there is no appointments option)\n"
            "- human: a live receptionist talking to us (e.g. 'Good morning, Riverside Surgery, "
            "Sarah speaking'). A recording also says the surgery's name, so look for "
            "conversational, one-off speech rather than scripted announcements\n"
            "- closed: the surgery is closed or the queue is full and the call will not be answered"
        )
        try:
            response = await asyncio.wait_for(
                self._client.models.generate_content(
                    model=settings.gemini_model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": GeminiVerdict,
                    },
                ),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("Gemini classifier timed out; using keyword fallback")
            return None
        except Exception as exc:
            log.warning("Gemini classifier unavailable (%s); using keyword fallback", type(exc).__name__)
            return None

        try:
            parsed = GeminiVerdict.model_validate(response.parsed or json.loads(response.text or ""))
        except (AttributeError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            log.warning("Gemini classifier returned an unparseable result; using keyword fallback")
            return None

        return Verdict(Heard(parsed.label))


def _menu_digit_for_appointments(text: str) -> str | None:
    """'For appointments, press 1. For prescriptions, press 2.' -> '1'."""
    t = text.lower()
    for clause in re.split(r"[.;]", t):
        if re.search(r"appointment|book|reception|other enquir|all other", clause):
            m = re.search(r"press (\d)", clause)
            if m:
                return m.group(1)
    return None

