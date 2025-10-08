"""
Configuration module.

Reads settings from environment variables (.env supported) and exposes
typed constants for application use. No secrets should be hardcoded here.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    # Optional: load from .env if present
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    return value


# External API keys and tokens
elevenlabs_api_key: Optional[str] = _get_env("ELEVENLABS_API_KEY")
openai_api_key: Optional[str] = _get_env("OPENAI_API_KEY")
bot_token: Optional[str] = _get_env("TELEGRAM_BOT_TOKEN")


# Application flags and limits
DEBUG: bool = _get_env("DEBUG", "false").lower() in ("1", "true", "yes", "y")
MAX_TTS_CHARS: int = int(_get_env("MAX_TTS_CHARS", "1200") or 1200)


# Logging
LOG_LEVEL: str = _get_env("LOG_LEVEL", "INFO")