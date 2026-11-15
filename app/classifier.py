"""Decide what we are hearing while waiting: IVR menu, recording, or a human.

Two layers:
1. Fast rules: catch the obvious recorded phrases ("you are number 5 in the
   queue") and anything we've already heard before. Recordings repeat; people
   don't.
2. Keywords: for everything else, find the appointments key in IVR menus and
   spot a receptionist's greeting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

log = logging.getLogger(__name__)


class Heard(str, Enum):
    HOLD = "hold"            # music, silence, or a recorded queue message
    IVR_MENU = "ivr_menu"    # "press 1 for appointments..."
    HUMAN = "human"          # a live receptionist
    CLOSED = "closed"        # "the surgery is now closed" / "lines are full, call back"


@dataclass
class Verdict:
    heard: Heard
    digits: str | None = None
    reason: str = ""


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

    def _is_repeat(self, text: str) -> bool:
        return any(SequenceMatcher(None, text.lower(), old.lower()).ratio() > 0.8 for old in self.heard_before)

    def quick_rules(self, text: str) -> Verdict | None:
        t = text.lower()
        if any(re.search(p, t) for p in CLOSED_PATTERNS):
            return Verdict(Heard.CLOSED, reason="closed/queue-full message")
        if self._is_repeat(text):
            return Verdict(Heard.HOLD, reason="repeated message, so a recording")
        if any(re.search(p, t) for p in QUEUE_PATTERNS) and not MENU_PATTERN.search(t):
            return Verdict(Heard.HOLD, reason="queue announcement")
        return None

    async def classify(self, text: str) -> Verdict:
        text = text.strip()
        if not text:
            return Verdict(Heard.HOLD, reason="silence/music")
        verdict = self.quick_rules(text)
        if verdict is None:
            verdict = self.keyword_rules(text)
        self.heard_before.append(text)
        return verdict

    def keyword_rules(self, text: str) -> Verdict:
        """Crude keyword guess (good enough for a demo)."""
        menu = _menu_digit_for_appointments(text)
        if menu:
            return Verdict(Heard.IVR_MENU, digits=menu, reason="keyword menu match")
        if re.search(r"\b(speaking|how can i help|how may i help|can i help)\b", text, re.I):
            return Verdict(Heard.HUMAN, reason="greeting keywords")
        return Verdict(Heard.HOLD, reason="no rule matched")



def _menu_digit_for_appointments(text: str) -> str | None:
    """'For appointments, press 1. For prescriptions, press 2.' -> '1'."""
    t = text.lower()
    for clause in re.split(r"[.;]", t):
        if re.search(r"appointment|book|reception|other enquir|all other", clause):
            m = re.search(r"press (\d)", clause)
            if m:
                return m.group(1)
    return None

