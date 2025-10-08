from __future__ import annotations

from typing import Any, Optional
from elevenlabs.client import ElevenLabs
import config


# Singleton client instance
_client: Optional[ElevenLabs] = None


def _get_client() -> ElevenLabs:
    """Returns a singleton ElevenLabs client instance."""
    global _client
    if _client is None:
        if not config.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")
        _client = ElevenLabs(api_key=config.elevenlabs_api_key)
    return _client


def get_all_voices() -> Any:
    """Возвращает объект со списком голосов ElevenLabs."""
    client = _get_client()
    return client.voices.get_all()


def generate_audio(text: str, voice_id: str, output_format: str = "mp3_44100_128") -> bytes:
    """Генерирует озвучку и возвращает байты аудио.

    Поддерживаемые форматы см. в ElevenLabs API (например: mp3_44100_128, opus_48000_64).
    """
    client = _get_client()
    audio_stream = client.text_to_speech.convert(
        voice_id=voice_id,
        model_id="eleven_multilingual_v2",
        text=text,
        output_format=output_format,
    )
    chunks: list[bytes] = []
    for chunk in audio_stream:
        chunks.append(chunk)
    return b"".join(chunks)
