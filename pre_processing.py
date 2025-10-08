"""Utilities for preparing text for TTS via OpenAI prompts.

Provides:
- OpenAI-based normalization by style ("announcer"/"conversational").
"""

import re
import json
from pathlib import Path
from typing import Optional, Dict
from openai import OpenAI
import os
try:
    import config as _app_config
except Exception:
    _app_config = None


# Constants
OPENAI_MODEL = "gpt-4o-mini"
"""OpenAI model for text preprocessing.
Supported models:
- gpt-4o-mini: Fast and cost-effective (recommended)
- gpt-4o: High quality, slower, more expensive
- gpt-4-turbo: Balanced performance
- gpt-3.5-turbo: Fastest, lowest quality
Note: GPT-5 will be supported when released.
"""

OPENAI_TEMPERATURE = 0.2
"""Temperature for OpenAI API (0.0-2.0).
Lower values (0.1-0.3) = more deterministic, consistent
Higher values (0.7-1.0) = more creative, varied
"""


def _load_prompt(style: str) -> str:
    path = Path(__file__).with_name("pre_processing.json")
    try:
        data: Dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        if style == "announcer":
            return data.get("announcer", "")
        if style == "conversational":
            return data.get("conversational", "")
    except Exception:
        pass
    return ""


def prepare_for_tts(text: str, style: str = "announcer") -> str:
    """Normalize text for TTS using OpenAI and a style-specific prompt.

    Falls back to the original text on any error or missing configuration.
    """

    api_key: Optional[str] = os.getenv("OPENAI_API_KEY") or (
        getattr(_app_config, "openai_api_key", None) if _app_config else None
    )
    if not api_key:
        # Если ключа нет — вернём исходный текст без падения
        return text

    system_prompt = _load_prompt(style)
    if not system_prompt:
        return text

    client = OpenAI(api_key=api_key)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=OPENAI_TEMPERATURE,
        )
        content: Optional[str] = resp.choices[0].message.content if resp.choices else None
        if not content:
            return text
        # Нормализация пробелов на выходе
        processed = re.sub(r"\s+", " ", content).strip()
        
        # Логирование для отладки
        try:
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"OpenAI preprocessing [{style}]:")
            logger.debug(f"  Input: {text[:100]}...")
            logger.debug(f"  Output: {processed[:100]}...")
        except Exception:
            pass
            
        return processed
    except Exception as e:
        try:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"OpenAI preprocessing error: {e}")
        except Exception:
            pass
        return text




if __name__ == "__main__":
    # Self-test intentionally removed in production. Keep module import-only.
    pass
