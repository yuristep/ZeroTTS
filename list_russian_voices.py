"""Утилита для вывода голосов ElevenLabs (все и только русские)."""

import os
from typing import Any, Dict, List
import requests
from dotenv import load_dotenv
import config
from logging_setup import setup_logging

load_dotenv()
logger = setup_logging(config.LOG_LEVEL)

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY") or getattr(config, "elevenlabs_api_key", None)
if not ELEVEN_API_KEY:
    raise ValueError("ELEVENLABS_API_KEY is not set (env or config)")

url = "https://api.elevenlabs.io/v1/voices"
headers = {"xi-api-key": ELEVEN_API_KEY}

print("🔍 Получаем список голосов ElevenLabs...")

try:
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
except Exception as exc:
    logger.error("Failed to fetch voices: %r", exc)
    raise SystemExit(1)

data = response.json()
voices: List[Dict[str, Any]] = data.get("voices", [])

# Полный список
print(f"\nВсего голосов: {len(voices)}\n")
print(f"{'Имя':20} | {'Пол':8} | {'Язык':8} | {'Категория':10} | voice_id")
print("-" * 80)
for v in voices:
    name = v.get("name")
    vid = v.get("voice_id")
    labels = v.get("labels", {}) or {}
    gender = labels.get("gender", "—")
    category = v.get("category", "—")
    lang = labels.get("language", "—")
    print(f"{name:20} | {gender:8} | {lang:8} | {category:10} | {vid}")

russian_voices = [
    v for v in voices
    if (v.get("labels", {}) or {}).get("language", "").lower() in ("ru", "russian", "русский")
]

print("\nРусские голоса:")
if not russian_voices:
    print("⚠️ Не найдено русских голосов.")
else:
    print(f"(всего: {len(russian_voices)})\n")
    print(f"{'Имя':20} | {'Пол':8} | {'Язык':8} | {'Категория':10} | voice_id")
    print("-" * 80)
    for v in russian_voices:
        name = v.get("name")
        vid = v.get("voice_id")
        labels = v.get("labels", {}) or {}
        gender = labels.get("gender", "—")
        category = v.get("category", "—")
        lang = labels.get("language", "—")
        print(f"{name:20} | {gender:8} | {lang:8} | {category:10} | {vid}")
