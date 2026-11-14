"""Local-only speech in and out.

If the optional local models are not installed these degrade to
text-only. There is no cloud fallback anywhere in this codebase.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
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


TTS_DIR = Path(__file__).resolve().parent.parent / "data" / "tts"

# One long-lived Windows SAPI process, so each phrase costs ~0.3s rather than a
# PowerShell start-up. Text and paths go in via files we name, never the command line.
_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo }
$v = $voices | Where-Object { $_.Name -like '*Hazel*' } | Select-Object -First 1
if (-not $v) { $v = $voices | Where-Object { $_.Culture.Name -eq 'en-GB' } | Select-Object -First 1 }
if ($v) { $s.SelectVoice($v.Name) }
$s.Rate = -1
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(8000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
[Console]::Out.WriteLine('ready')
[Console]::Out.Flush()
while ($null -ne ($line = [Console]::In.ReadLine())) {
  try {
    $parts = $line.Split("`t")
    $text = [System.IO.File]::ReadAllText($parts[0], [System.Text.Encoding]::UTF8)
    $s.SetOutputToWaveFile($parts[1], $fmt)
    $s.Speak($text)
    $s.SetOutputToNull()
    [Console]::Out.WriteLine('ok')
  } catch {
    $s.SetOutputToNull()
    [Console]::Out.WriteLine('err')
  }
  [Console]::Out.Flush()
}
"""

_sapi_proc: Optional[subprocess.Popen] = None
_sapi_lock = threading.Lock()


def _sapi_available() -> bool:
    return sys.platform == "win32" and shutil.which("powershell") is not None


def local_tts_available() -> bool:
    """True if the agent's phone voice can be generated on this device."""
    return TTS_AVAILABLE or _sapi_available()


def _sapi_worker() -> subprocess.Popen:
    global _sapi_proc
    if _sapi_proc is None or _sapi_proc.poll() is not None:
        encoded = base64.b64encode(_SAPI_SCRIPT.encode("utf-16-le")).decode()
        _sapi_proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if _sapi_proc.stdout.readline().strip() != "ready":
            raise RuntimeError("Windows speech engine did not start")
    return _sapi_proc


def _sapi_synthesise(text: str, out: Path) -> None:
    with _sapi_lock:
        proc = _sapi_worker()
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write(text)
            text_path = tmp.name
        try:
            proc.stdin.write(f"{text_path}\t{out}\n")
            proc.stdin.flush()
            if proc.stdout.readline().strip() != "ok":
                raise RuntimeError("Windows speech engine failed")
        finally:
            Path(text_path).unlink(missing_ok=True)


def tts_name(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def synthesise_to_file(text: str) -> Path:
    """Agent's phone voice, generated on this device and cached by text hash."""
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    out = TTS_DIR / f"{tts_name(text)}.wav"
    if out.exists() and out.stat().st_size > 44:
        return out
    if TTS_AVAILABLE:
        wav = kokoro_synthesise(text)
        if wav:
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(wav)
            tmp.replace(out)
            return out
    if not _sapi_available():
        raise RuntimeError("No local voice available on this device")
    tmp = out.with_suffix(".part.wav")
    _sapi_synthesise(text, tmp)
    tmp.replace(out)
    return out


def warm_phrases(phrases: list[str]) -> None:
    """Pre-generate likely lines in the background so the call doesn't wait on them."""
    def run():
        for p in phrases:
            try:
                synthesise_to_file(p)
            except Exception:
                return
    threading.Thread(target=run, daemon=True).start()


def stop_tts() -> None:
    global _sapi_proc
    if _sapi_proc and _sapi_proc.poll() is None:
        _sapi_proc.kill()
    _sapi_proc = None


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
