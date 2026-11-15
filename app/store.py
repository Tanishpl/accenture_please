"""In-memory session store. Fine for a hackathon; swap for Redis/Postgres later.

Health data is special-category data under UK GDPR, so nothing is written to
disk and terminal sessions are dropped after ten minutes.
"""

import asyncio
import time

from .models import TERMINAL, CallSession

_sessions: dict[str, CallSession] = {}
RETAIN_SECONDS = 600
NON_TERMINAL_RETAIN_SECONDS = 7200


def add(session: CallSession) -> None:
    _purge()
    _sessions[session.id] = session


def get(session_id: str) -> CallSession | None:
    return _sessions.get(session_id)


def by_call_sid(call_sid: str) -> CallSession | None:
    return next((s for s in _sessions.values() if s.call_sid == call_sid), None)


def _purge() -> None:
    now = time.time()
    for sid, s in list(_sessions.items()):
        last = s.created_at + (s.events[-1]["t"] if s.events else 0)
        if (
            (s.status in TERMINAL and now - last > RETAIN_SECONDS)
            or (s.status not in TERMINAL and now - s.created_at > NON_TERMINAL_RETAIN_SECONDS)
        ):
            del _sessions[sid]


async def cleanup_loop(interval_seconds: int = 60) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        _purge()
