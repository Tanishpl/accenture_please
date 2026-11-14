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

    elevenlabs_api_key: str = _get("ELEVENLABS_API_KEY")
    elevenlabs_agent_id: str = _get("ELEVENLABS_AGENT_ID")

    # Speech-to-text used while waiting on hold (cheap, so we don't pay for the
    # voice agent during a 40 minute queue).
    deepgram_api_key: str = _get("DEEPGRAM_API_KEY")

    # Classifier that decides "hold music / recorded message / IVR menu / human".
    anthropic_model: str = _get("ANTHROPIC_MODEL", "claude-opus-5-5")

    # Give up waiting after this many minutes.
    max_hold_minutes: int = int(_get("MAX_HOLD_MINUTES", "90"))

    @property
    def public_ws_url(self) -> str:
        return self.public_url.replace("https://", "wss://").replace("http://", "ws://")


settings = Settings()
