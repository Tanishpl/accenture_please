"""Local-only speech in and out.

If the optional local models are not installed these degrade to
text-only. There is no cloud fallback anywhere in this codebase.
"""
from __future__ import annotations

import io
import os
import threading
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
# tiny.en for very weak devices, small.en if there's plenty of RAM.
WHISPER_MODEL = os.environ.get("GP_AGENT_WHISPER", "base.en")


_whisper_lock = threading.Lock()


def load_whisper() -> None:
    """Load (and on first run, download) the model. Called at startup so the first answer isn't slow."""
    global _whisper
    with _whisper_lock:
        if _whisper is None and STT_AVAILABLE:
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def transcribe(audio_path: str) -> str:
    """Speech in, on-device via faster-whisper."""
    if not STT_AVAILABLE:
        raise RuntimeError("faster-whisper is not installed; use typed input.")
    load_whisper()
    # vad_filter drops silence, which is where Whisper tends to invent words.
    segments, _ = _whisper.transcribe(
        audio_path, language="en", beam_size=1, vad_filter=True,
        initial_prompt="A patient booking a GP appointment. Paracetamol, ibuprofen, weekday mornings.",
    )
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
