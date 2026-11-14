"""ElevenLabs text-to-speech, served to Twilio's <Play> as streamed MP3.

Generation starts the moment a reply is ready, before Twilio has even
received the TwiML, so audio is usually already flowing when Twilio asks for it.
Clips are keyed by their text, so stock phrases are only synthesised once.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator

import httpx

from .config import settings

log = logging.getLogger(__name__)

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
MAX_CLIPS = 200


class Clip:
    def __init__(self, text: str) -> None:
        self.text = text
        self.chunks: list[bytes] = []
        self.done = False
        self.failed = False
        self._changed = asyncio.Event()
        self._task = asyncio.create_task(self._generate())

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def _generate(self) -> None:
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
                        body = (await r.aread()).decode(errors="replace")[:300]
                        raise RuntimeError(f"ElevenLabs {r.status_code}: {body}")
                    async for chunk in r.aiter_bytes():
                        self.chunks.append(chunk)
                        self._notify()
        except Exception as exc:
            self.failed = True
            log.warning("Text-to-speech failed for %r: %s", self.text[:60], exc)
        finally:
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


def clip_url(text: str) -> str:
    """Start synthesising `text` (if not already cached) and return its public URL."""
    clip_id = hashlib.sha1(f"{settings.elevenlabs_voice_id}:{text}".encode()).hexdigest()[:16]
    clip = _clips.get(clip_id)
    if clip is None or clip.failed:
        _clips[clip_id] = Clip(text)
        while len(_clips) > MAX_CLIPS:
            _clips.popitem(last=False)
    return f"{settings.public_url}/audio/{clip_id}.mp3"


def get(clip_id: str) -> Clip | None:
    return _clips.get(clip_id)
