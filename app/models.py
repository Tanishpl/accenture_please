"""Data models: what the patient submits and the state of one call."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field, field_validator


def normalise_uk_phone(value: str) -> str:
    """Turn '07700 900123' / '01632 960000' / '+44 7700 900123' into E.164."""
    digits = re.sub(r"[^\d+]", "", value)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0"):
        digits = "+44" + digits[1:]
    if not re.fullmatch(r"\+\d{8,15}", digits):
        raise ValueError("Enter a valid phone number, e.g. 07700 900123")
    return digits


class PatientRequest(BaseModel):
    """Everything the agent may need to tell the receptionist."""

    full_name: str = Field(min_length=2, max_length=100)
    date_of_birth: str = Field(min_length=6, max_length=30)
    address: str = Field(min_length=5, max_length=200)
    patient_phone: str
    nhs_number: str = ""
    reason: str = Field(min_length=3, max_length=500)
    availability: str = Field(min_length=3, max_length=300)
    preferred_clinician: str = ""
    surgery_name: str = Field(min_length=2, max_length=100)
    surgery_phone: str
    allow_patient_callback: bool = True
    consent: bool

    @field_validator("patient_phone", "surgery_phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalise_uk_phone(v)

    @field_validator("nhs_number")
    @classmethod
    def _nhs(cls, v: str) -> str:
        v = re.sub(r"\s", "", v)
        if v and not re.fullmatch(r"\d{10}", v):
            raise ValueError("NHS number should be 10 digits")
        return v

    @field_validator("consent")
    @classmethod
    def _consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Consent is required to share your details with the surgery")
        return v

    def dynamic_variables(self) -> dict[str, str]:
        """Values substituted into {{placeholders}} in the ElevenLabs agent prompt."""
        return {
            "patient_name": self.full_name,
            "date_of_birth": self.date_of_birth,
            "address": self.address,
            "patient_phone": self.patient_phone,
            "nhs_number": self.nhs_number or "not provided",
            "reason": self.reason,
            "availability": self.availability,
            "preferred_clinician": self.preferred_clinician or "no preference",
            "surgery_name": self.surgery_name,
            "patient_callback_allowed": "yes" if self.allow_patient_callback else "no",
        }


class CallStatus(str, Enum):
    DIALLING = "dialling"
    ON_HOLD = "on_hold"
    TALKING = "talking_to_receptionist"
    HANDED_TO_PATIENT = "handed_to_patient"
    BOOKED = "booked"
    FAILED = "failed"
    ENDED = "ended"


TERMINAL = {CallStatus.BOOKED, CallStatus.FAILED, CallStatus.ENDED, CallStatus.HANDED_TO_PATIENT}


@dataclass
class Booking:
    date: str
    time: str
    clinician: str = ""
    location: str = ""
    notes: str = ""


@dataclass
class CallSession:
    patient: PatientRequest
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    call_sid: str | None = None
    status: CallStatus = CallStatus.DIALLING
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    booking: Booking | None = None
    outcome: str = ""
    events: list[dict] = field(default_factory=list)

    def log(self, kind: str, text: str) -> None:
        self.events.append({"t": round(time.time() - self.created_at, 1), "kind": kind, "text": text})

    def public_view(self) -> dict:
        """What the status page is allowed to see (no personal details)."""
        return {
            "id": self.id,
            "status": self.status.value,
            "surgery": self.patient.surgery_name,
            "waited_seconds": round((self.answered_at or time.time()) - self.created_at),
            "booking": self.booking.__dict__ if self.booking else None,
            "outcome": self.outcome,
            "events": self.events[-50:],
        }
