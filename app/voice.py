"""ElevenLabs text-to-speech, served to Twilio's <Play> as streamed MP3.

Generation starts the moment a reply is ready, before Twilio has even
received the TwiML, so audio is usually already flowing when Twilio asks for it.
Clips are keyed by their text, so stock phrases are only synthesised once.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator

import httpx

from .config import settings

log = logging.getLogger(__name__)

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
MAX_CLIPS = 200
CLIP_RETAIN_SECONDS = 600
_TONE_TAG_RE = re.compile(
    r"\[(?:very\s+)?(?:happy|sad|laughs?|laughing|whispers?|whispering|"
    r"shouts?|shouting|excited|angry|softly|quietly|sighs?|chuckles?|"
    r"hesitates?|breathes?|cheerful|warmly)\]",
    re.IGNORECASE,
)


def strip_tone_tags(text: str) -> str:
    """Remove common short stage-direction tags without changing ordinary brackets."""
    return re.sub(r"\s{2,}", " ", _TONE_TAG_RE.sub("", text)).strip()


class Clip:
    def __init__(self, text: str, content_hash: str = "") -> None:
        self.text = strip_tone_tags(text)
        self.content_hash = content_hash
        self.created_at = time.monotonic()
        self.chunks: list[bytes] = []
        self.done = False
        self.failed = False
        self._changed = asyncio.Event()
        self._task = asyncio.create_task(self._generate())

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def _generate(self) -> None:
        error_category = "unexpected"
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                async with http.stream(
                    "POST",
                    TTS_URL.format(voice_id=settings.elevenlabs_voice_id),
                    params={"output_format": "mp3_22050_32"},
                    headers={"xi-api-key": settings.elevenlabs_api_key},
                    json={"text": self.text, "model_id": settings.elevenlabs_model},
                ) as r:
                    if r.status_code >= 400:
                        error_category = "http_status"
                        raise RuntimeError
                    async for chunk in r.aiter_bytes():
                        self.chunks.append(chunk)
                        self._notify()
        except httpx.TimeoutException:
            error_category = "timeout"
            self.failed = True
        except httpx.HTTPError:
            error_category = "http_error"
            self.failed = True
        except Exception:
            self.failed = True
        finally:
            if self.failed:
                log.warning("Text-to-speech failed: %s", error_category)
            self.done = True
            self._notify()

    async def wait_for_audio(self) -> bool:
        """True once the first audio arrives; False if generation failed first."""
        while not self.chunks and not self.done:
            await self._changed.wait()
        return bool(self.chunks)

    async def stream(self) -> AsyncIterator[bytes]:
        i = 0
        while True:
            while i < len(self.chunks):
                yield self.chunks[i]
                i += 1
            if self.done:
                return
            await self._changed.wait()


_clips: OrderedDict[str, Clip] = OrderedDict()
_clip_ids: dict[str, str] = {}


def _content_hash(text: str) -> str:
    return hashlib.sha256(f"{settings.elevenlabs_voice_id}:{text}".encode()).hexdigest()


def _purge_expired(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    for clip_id, clip in list(_clips.items()):
        if now - clip.created_at >= CLIP_RETAIN_SECONDS:
            del _clips[clip_id]
            if _clip_ids.get(clip.content_hash) == clip_id:
                del _clip_ids[clip.content_hash]


def clip_url(text: str) -> str:
    """Start synthesising `text` (if not already cached) and return its public URL."""
    text = strip_tone_tags(text)
    _purge_expired()
    content_hash = _content_hash(text)
    clip_id = _clip_ids.get(content_hash)
    clip = _clips.get(clip_id) if clip_id else None
    if clip is None or clip.failed:
        if clip_id:
            _clips.pop(clip_id, None)
            _clip_ids.pop(content_hash, None)
        clip_id = secrets.token_urlsafe(16)
        _clip_ids[content_hash] = clip_id
        _clips[clip_id] = Clip(text, content_hash)
        while len(_clips) > MAX_CLIPS:
            evicted_id, evicted = _clips.popitem(last=False)
            if _clip_ids.get(evicted.content_hash) == evicted_id:
                del _clip_ids[evicted.content_hash]
    return f"{settings.public_url}/audio/{clip_id}.mp3"


def get(clip_id: str) -> Clip | None:
    _purge_expired()
    return _clips.get(clip_id)
