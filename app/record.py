"""The structured record, SQLite storage and the local audit log.

Everything in this file touches disk only on the patient's own device.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "record.db"

# The only record fields the voice path may read. Enforced in app/speech.py.
SPEAKABLE = {"spoken_summary", "reason_for_contact", "preferences"}


@dataclass
class ClinicalDetail:
    description: str = ""
    duration: str = ""
    trajectory: str = ""
    seen_before_for_this: bool = False
    taking_anything: str = ""


@dataclass
class Preferences:
    urgency: str = ""
    appointment_type: str = ""
    times: list[str] = field(default_factory=list)
    preferred_clinician: Optional[str] = None


@dataclass
class Record:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    patient_ref: str = "local-only"
    reason_for_contact: str = ""
    clinical_detail: ClinicalDetail = field(default_factory=ClinicalDetail)
    preferences: Preferences = field(default_factory=Preferences)
    access_needs: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    spoken_summary: str = ""
    confirmed_by_patient: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Record":
        clinical = ClinicalDetail(**data.get("clinical_detail", {}))
        prefs = Preferences(**data.get("preferences", {}))
        return Record(
            request_id=data.get("request_id", str(uuid.uuid4())),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            patient_ref=data.get("patient_ref", "local-only"),
            reason_for_contact=data.get("reason_for_contact", ""),
            clinical_detail=clinical,
            preferences=prefs,
            access_needs=data.get("access_needs", []),
            red_flags=data.get("red_flags", []),
            spoken_summary=data.get("spoken_summary", ""),
            confirmed_by_patient=data.get("confirmed_by_patient", False),
        )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                request_id TEXT PRIMARY KEY,
                created_at TEXT,
                data TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT,
                timestamp TEXT,
                speaker TEXT,
                said TEXT,
                heard TEXT,
                agreed TEXT,
                unanswered TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                credential_id TEXT PRIMARY KEY,
                data TEXT,
                revoked INTEGER DEFAULT 0
            )
            """
        )


def save_record(record: Record) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO records (request_id, created_at, data) VALUES (?, ?, ?)",
            (record.request_id, record.created_at, json.dumps(record.to_dict())),
        )


def get_record(request_id: str) -> Optional[Record]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM records WHERE request_id = ?", (request_id,)
        ).fetchone()
    if not row:
        return None
    return Record.from_dict(json.loads(row[0]))


def append_audit(
    request_id: str,
    speaker: str,
    said: str = "",
    heard: str = "",
    agreed: str = "",
    unanswered: str = "",
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (request_id, timestamp, speaker, said, heard, agreed, unanswered)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                datetime.now(timezone.utc).isoformat(),
                speaker,
                said,
                heard,
                agreed,
                unanswered,
            ),
        )


def get_audit(request_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, speaker, said, heard, agreed, unanswered
            FROM audit_log WHERE request_id = ? ORDER BY id ASC
            """,
            (request_id,),
        ).fetchall()
    return [
        {
            "timestamp": r[0],
            "speaker": r[1],
            "said": r[2],
            "heard": r[3],
            "agreed": r[4],
            "unanswered": r[5],
        }
        for r in rows
    ]
