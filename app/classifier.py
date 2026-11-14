"""Decide what we are hearing while waiting: IVR menu, recording, or a human.

Two layers:
1. Fast rules: catch the obvious recorded phrases ("you are number 5 in the
   queue") and anything we've already heard before. Recordings repeat; people
   don't.
2. Claude: for everything else, especially "is this a live receptionist?" and
   "which key gets me to appointments?".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

import anthropic

from .config import settings

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
        # Picks up ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
        # If none work we drop to keyword rules on the first failed request.
        self._client: anthropic.AsyncAnthropic | None = anthropic.AsyncAnthropic()

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
            verdict = await self._ask_claude(text) if self._client else self._fallback(text)
        self.heard_before.append(text)
        return verdict

    def _fallback(self, text: str) -> Verdict:
        """No Claude credentials: crude keyword guess (good enough for a demo)."""
        menu = _menu_digit_for_appointments(text)
        if menu:
            return Verdict(Heard.IVR_MENU, digits=menu, reason="keyword menu match")
        if re.search(r"\b(speaking|how can i help|how may i help|can i help)\b", text, re.I):
            return Verdict(Heard.HUMAN, reason="greeting keywords")
        return Verdict(Heard.HOLD, reason="no rule matched")

    async def _ask_claude(self, text: str) -> Verdict:
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
            response = await self._client.beta.messages.create(
                model=settings.anthropic_model,
                max_tokens=1024,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": "low",
                    "format": {
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "heard": {"type": "string", "enum": [h.value for h in Heard]},
                                "digits": {"type": ["string", "null"]},
                                "reason": {"type": "string"},
                            },
                            "required": ["heard", "digits", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AuthenticationError:
            log.warning("No valid Anthropic credentials; using keyword rules for this call")
            self._client = None
            return self._fallback(text)
        except anthropic.RateLimitError:
            log.warning("Claude rate limited; using keyword fallback")
            return self._fallback(text)
        except anthropic.APIStatusError as exc:
            log.warning("Claude API error %s; using keyword fallback", exc.status_code)
            return self._fallback(text)
        except anthropic.APIConnectionError:
            log.warning("Claude unreachable; using keyword fallback")
            return self._fallback(text)
        except anthropic.AnthropicError as exc:  # e.g. no credentials configured at all
            log.warning("Claude unavailable (%s); using keyword rules for this call", exc)
            self._client = None
            return self._fallback(text)

        if response.stop_reason != "end_turn":
            log.warning("Classifier stopped with %s; using keyword fallback", response.stop_reason)
            return self._fallback(text)
        raw = next(b.text for b in response.content if b.type == "text")
        data = json.loads(raw)
        digits = data.get("digits")
        if digits:
            digits = re.sub(r"[^0-9*#]", "", digits.replace("star", "*").replace("hash", "#")) or None
        return Verdict(Heard(data["heard"]), digits=digits, reason=data.get("reason", ""))


def _menu_digit_for_appointments(text: str) -> str | None:
    """'For appointments, press 1. For prescriptions, press 2.' -> '1'."""
    t = text.lower()
    for clause in re.split(r"[.;]", t):
        if re.search(r"appointment|book|reception|other enquir|all other", clause):
            m = re.search(r"press (\d)", clause)
            if m:
                return m.group(1)
    return None

