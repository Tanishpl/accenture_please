"""Local-only speech in and out.

If the optional local models are not installed these degrade to
text-only. There is no cloud fallback anywhere in this codebase.
"""
from __future__ import annotations

import io
import wave
from typing import Optional

from app.record import SPEAKABLE

try:
    from faster_whisper import WhisperModel
    STT_AVAILABLE = True
except ImportError:
    STT_AVAILABLE = False

try:
    from kokoro import KPipeline
    import numpy as np
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

_whisper = None
_tts = None
KOKORO_SAMPLE_RATE = 24000


def transcribe(audio_path: str) -> str:
    """Speech in, on-device via faster-whisper base.en."""
    global _whisper
    if not STT_AVAILABLE:
        raise RuntimeError("faster-whisper is not installed; use typed input.")
    if _whisper is None:
        _whisper = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(audio_path)
    return " ".join(seg.text.strip() for seg in segments).strip()


def kokoro_synthesise(text: str) -> Optional[bytes]:
    """Speech out, on-device via Kokoro 82M. Returns WAV bytes, or None."""
    global _tts
    if not TTS_AVAILABLE:
        return None
    if _tts is None:
        _tts = KPipeline(lang_code="b")  # British English
    chunks = [audio for _, _, audio in _tts(text, voice="bf_emma")]
    if not chunks:
        return None
    pcm = (np.clip(np.concatenate([np.asarray(c) for c in chunks]), -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(KOKORO_SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def speakable_text(record: dict, field: str) -> str:
    if field not in SPEAKABLE:
        raise ValueError(f"{field} is not speakable")
    value = record[field]
    if isinstance(value, dict):
        bits = [value.get("urgency"), value.get("appointment_type"), ", ".join(value.get("times") or [])]
        if value.get("preferred_clinician"):
            bits.append(f"with {value['preferred_clinician']}")
        return ". ".join(b for b in bits if b)
    return str(value)


def speak(record: dict, field: str) -> Optional[bytes]:
    """The whole privacy argument: only allowlisted fields reach the voice."""
    return kokoro_synthesise(speakable_text(record, field))
