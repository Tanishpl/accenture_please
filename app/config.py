"""Settings loaded from environment variables (see .env.example)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Public HTTPS base URL Twilio can reach (e.g. an ngrok URL), no trailing slash.
    public_url: str = _get("PUBLIC_URL").rstrip("/")

    twilio_account_sid: str = _get("TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = _get("TWILIO_AUTH_TOKEN")
    twilio_phone_number: str = _get("TWILIO_PHONE_NUMBER")

    # ElevenLabs: the agent's brain (text-only agent) and its voice (text-to-speech).
    elevenlabs_api_key: str = _get("ELEVENLABS_API_KEY")
    # Default: "George", a British male premade voice.
    elevenlabs_voice_id: str = _get("ELEVENLABS_VOICE_ID") or "JBFqnCBsd6RMkjVDRZzb"
    elevenlabs_model: str = _get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
    # Text-only ElevenLabs agent that writes the replies (scripts/setup_agent.py).
    elevenlabs_agent_id: str = _get("ELEVENLABS_AGENT_ID")

    # Optional: Claude classifies what we hear on hold. Without a key, keyword rules are used.
    anthropic_model: str = _get("ANTHROPIC_MODEL", "claude-opus-5-5")

    # Twilio trial accounts end the call after this many <Gather>/<Redirect>
    # callbacks. Every phrase heard counts as one. Set to 0 on a paid account.
    max_hops: int = int(_get("MAX_HOPS", "10"))

    # Twilio waits 5s (trial) for our TwiML; leave room for the network round trip.
    reply_timeout_seconds: float = float(_get("REPLY_TIMEOUT_SECONDS", "3.8"))


settings = Settings()
