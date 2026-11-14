"""Scripted receptionist for the demo.

This plays the practice side only. In the live demo its lines can be
voiced with ElevenLabs; it never sees anything the patient said, only
what the agent says down the line.
"""
from __future__ import annotations

PRACTICE_NAME = "Riverside Surgery"
PRACTICE_NUMBER = "01632 960 123"

IVR = ("ivr", f"Thank you for calling {PRACTICE_NAME}. For appointments, press 1. For prescriptions, press 2.")
HOLD = ("hold", "You are number 4 in the queue. Please hold.")
HOLD_MUSIC = ("hold", "[hold music]")
ANSWER = ("human", f"Good morning, {PRACTICE_NAME}, Sarah speaking.")

SCENARIOS: dict[str, list[tuple[str, str]]] = {
    # Exercises every row of the fallback table in one call.
    "full": [
        IVR, HOLD, HOLD_MUSIC, ANSWER,
        ("human", "Okay. What's it about?"),
        ("human", "Can you give me her date of birth and first line of address?"),
        ("human", "And does it sound like she needs to be seen today?"),
        ("human", "Does she want to come in or have a phone call?"),
        ("human", "What's her email address, for the confirmation?"),
        ("human", "When suits her?"),
        ("human", "I've got Thursday at 9:20 with Dr Shah. Would that work?"),
        ("human", "Yes, that's right. That's booked."),
    ],
    "simple": [
        IVR, HOLD_MUSIC, ANSWER,
        ("human", "What's it about?"),
        ("human", "When suits her?"),
        ("human", "I can do Wednesday at 10:40 with the nurse practitioner."),
        ("human", "Yes, that's booked in."),
    ],
    "decline": [
        IVR, HOLD_MUSIC, ANSWER,
        ("human", "Sorry, we don't take bookings from automated systems. She'll need to ring herself."),
    ],
    "garbled": [
        IVR, HOLD_MUSIC, ANSWER,
        ("human", "Mmhm, sorry, hang on, the system's..."),
        ("human", "...right, so, er..."),
        ("human", "...bear with..."),
    ],
    "voicemail": [
        IVR,
        ("voicemail", "The surgery is currently closed. Our opening hours are 8am to 6:30pm."),
    ],
}


def script(scenario: str) -> list[tuple[str, str]]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    return list(SCENARIOS[scenario])
