"""A mocked delegation credential.

The agent proves authority rather than reciting facts anyone could know.
There is no real National proxy service integration here -- this is a
policy ask, labelled as mocked, not a claim that it exists. See spec
section 11.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from app.record import _connect, init_db

AUTHORISED_ACTIONS = ["book_appointment", "cancel_appointment", "order_repeat"]


def issue_credential(practice: str = "GP practice") -> dict:
    init_db()
    credential = {
        "credential_id": f"mock-{uuid.uuid4().hex[:4]}",
        "issued_by": f"{practice} via National proxy service",
        "patient_ref": "held by practice, not by the agent",
        "authorised_actions": AUTHORISED_ACTIONS,
        "scope": "single practice",
        "expires": (datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
        "revocable_by_patient": True,
        "signature": "mocked for demo",
    }
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO credentials (credential_id, data, revoked) VALUES (?, ?, 0)",
            (credential["credential_id"], json.dumps(credential)),
        )
    return credential


def revoke_credential(credential_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE credentials SET revoked = 1 WHERE credential_id = ?", (credential_id,)
        )
    return cur.rowcount > 0


def is_authorised(credential_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT revoked FROM credentials WHERE credential_id = ?", (credential_id,)
        ).fetchone()
    return bool(row) and row[0] == 0


def mock_practice_endpoint(credential: dict) -> dict:
    """Stands in for the practice-side authorisation check."""
    if not is_authorised(credential["credential_id"]):
        return {"status": "revoked"}
    return {"status": "authorised"}
