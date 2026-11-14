"""Red flag, distress and opinion-seeking detection.

Pattern match only. Nothing here scores severity or gives clinical
advice: it detects and redirects.
"""
from __future__ import annotations

import re

_RED_FLAG_PATTERNS: dict[str, re.Pattern] = {
    "chest pain or pressure": re.compile(r"chest.{0,15}(pain|pressure|tight|crush)|(pain|pressure|tightness) in (my |the )?chest", re.I),
    "difficulty breathing": re.compile(r"(can'?t|cannot|struggling to|hard to) (breathe|catch my breath)|difficulty breathing|short(ness)? of breath|gasping", re.I),
    "sudden weakness or drooping on one side": re.compile(r"(face|mouth|arm|side).{0,20}droop|droop.{0,20}(face|mouth|side)|sudden(ly)? weak|numb(ness)? (down|on) one side", re.I),
    "sudden confusion or trouble speaking": re.compile(r"sudden(ly)? confus|slurr|(can'?t|cannot|trouble) (speak|talk|get my words)", re.I),
    "sudden severe headache": re.compile(r"(sudden|thunderclap|worst).{0,20}headache", re.I),
    "heavy bleeding that will not stop": re.compile(r"bleed.{0,25}(won'?t|will not|doesn'?t|does not|not) stop|heavy bleeding|bleeding heavily", re.I),
    "baby or small child floppy or will not wake": re.compile(r"(baby|child|toddler|son|daughter).{0,30}(floppy|won'?t wake|will not wake|not waking|unresponsive)", re.I),
}

_DISTRESS_PATTERNS: list[re.Pattern] = [
    re.compile(r"suicid", re.I),
    re.compile(r"self[\s-]?harm|hurt(ing)? myself|cut(ting)? myself", re.I),
    re.compile(r"(can'?t|cannot) go on", re.I),
    re.compile(r"(want|going) to (die|end it|end my life)", re.I),
    re.compile(r"kill(ing)? myself|end it all|no reason to live", re.I),
]

_OPINION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(is|could) (it|this|that) (be )?(serious|bad|dangerous|cancer|nothing)", re.I),
    re.compile(r"what do you think (it is|it could be|is wrong)", re.I),
    re.compile(r"should i (be worried|worry|go to a&e|take)", re.I),
    re.compile(r"what (is|could) (it|this) be", re.I),
]


def check_red_flags(text: str) -> list[str]:
    """Pattern match only. Never scores severity."""
    return [label for label, pattern in _RED_FLAG_PATTERNS.items() if pattern.search(text)]


def check_distress(text: str) -> bool:
    return any(p.search(text) for p in _DISTRESS_PATTERNS)


def asks_for_opinion(text: str) -> bool:
    return any(p.search(text) for p in _OPINION_PATTERNS)


RED_FLAG_MESSAGE = (
    "This needs urgent help now. Ring 999. If you are not sure, ring 111. "
    "I have not booked anything and I will not call the surgery."
)

DISTRESS_MESSAGE = (
    "I'm going to stop the booking here. If you are thinking about harming "
    "yourself, you can talk to Samaritans any time, free, on 116 123. If you "
    "are in immediate danger, ring 999. I can help you contact your GP "
    "directly as an urgent matter instead of an automated call."
)

OPINION_MESSAGE = "That's one for the doctor to say, I'm not able to give an opinion."
