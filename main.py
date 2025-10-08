import telebot
import io
import time
import httpx
import re
from typing import Optional, Dict, Tuple, List, Any, DefaultDict
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass
from functools import wraps

import config
from logging_setup import setup_logging
from voice import get_all_voices, generate_audio
from elevenlabs.core.api_error import ApiError
from telebot.apihelper import ApiTelegramException
from pre_processing import prepare_for_tts

logger = setup_logging(config.LOG_LEVEL)

# Constants
ELEVENLABS_USER_API_URL = "https://api.elevenlabs.io/v1/user"
"""ElevenLabs API endpoint for user quota information."""

CREDITS_PER_CHARS = 10
"""Number of characters per 1 ElevenLabs credit."""

VOICE_CACHE_TTL_SECONDS = 3600
"""Voice list cache TTL in seconds (1 hour)."""

USER_DATA_TTL_SECONDS = 86400
"""User data storage TTL in seconds (24 hours)."""

MODE_LABELS = {
    "off": "Без предобработки",
    "announcer": "ДИКТОРСКАЯ РЕЧЬ",
    "conversational": "РАЗГОВОРНЫЙ СТИЛЬ"
}


def measure_time(func):
    """Декоратор для измерения времени выполнения функций.
    
    Логирует время выполнения успешных вызовов и неудачных попыток.
    Полезен для мониторинга производительности критичных функций.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        func_name = func.__name__
        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - start
            if config.DEBUG or elapsed > 1.0:  # Логируем только долгие операции или в DEBUG
                logger.info(
                    "⏱️ Function %s completed in %.3fs",
                    func_name,
                    elapsed
                )
            return result
        except Exception as e:
            elapsed = time.time() - start
            logger.error(
                "❌ Function %s failed after %.3fs: %s",
                func_name,
                elapsed,
                str(e)
            )
            raise
    return wrapper


@dataclass(frozen=True)
class VoiceInfo:
    """Иммутабельная информация о голосе для TTS.
    
    Attributes:
        name: Название голоса
        voice_id: Уникальный идентификатор
        language: Язык голоса (ru, en, multi и т.д.)
        gender: Пол голоса (male, female)
    """
    name: str
    voice_id: str
    language: str
    gender: str
    
    @property
    def lang_priority(self) -> int:
        """Приоритет языка для сортировки (0=RU, 1=multi, 2=остальные)."""
        l = self.language.lower()
        if l in ("ru", "russian", "русский"):
            return 0
        if "multi" in l:
            return 1
        return 2
    
    def sort_key(self) -> Tuple[int, str]:
        """Ключ для сортировки: сначала по языку, потом по имени."""
        return (self.lang_priority, self.name.lower())


class BotConfig:
    """Bot-specific configuration constants."""
    
    # UI константы
    VOICE_BUTTONS_PER_ROW = 2
    """Количество кнопок голосов в ряду клавиатуры."""
    
    BACK_BUTTON_TEXT = "◀️ Назад"
    """Текст кнопки возврата в главное меню."""
    
    VOICE_SELECT_PROMPT = "Выберите голос:"
    """Приглашение выбора голоса."""
    
    SETTINGS_PROMPT = (
        "⚙️ <b>Настройки</b>\n\n"
        "📝 <b>Режим предобработки (псевдо-SSML для ElevenLabs):</b>\n"
        "🎭 <b>Формат ответа:</b>"
    )
    """Приглашение выбора режима псевдо-SSML и формата ответа."""
    
    # Callback data prefixes
    CALLBACK_CATEGORY = "cat:"
    CALLBACK_VOICE = "voice:"
    CALLBACK_SSML = "ssml:"
    CALLBACK_SETTINGS = "settings"
    CALLBACK_BACK = "back_to_menu"
    CALLBACK_FORMAT = "fmt:"

    # Supported response formats
    FORMAT_OGG = "ogg"
    FORMAT_MP3 = "mp3"
    FORMAT_BOTH = "both"


class RateLimiter:
    """Ограничитель частоты запросов для защиты от спама.
    
    Отслеживает количество запросов от каждого пользователя в заданном временном окне.
    """
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        """
        Args:
            max_requests: Максимальное количество запросов в окне
            window_seconds: Размер временного окна в секундах
        """
        self.max_requests = max_requests
        self.window = timedelta(seconds=window_seconds)
        self._requests: DefaultDict[int, List[datetime]] = defaultdict(list)
    
    def is_allowed(self, user_id: int) -> bool:
        """Проверяет, разрешен ли запрос от пользователя."""
        now = datetime.now()
        cutoff = now - self.window
        
        # Удаляем старые запросы
        self._requests[user_id] = [
            req_time for req_time in self._requests[user_id]
            if req_time > cutoff
        ]
        
        # Проверяем лимит
        if len(self._requests[user_id]) >= self.max_requests:
            logger.warning(
                "Rate limit exceeded for user_id=%s (%d requests in %ds)",
                user_id, len(self._requests[user_id]), self.window.total_seconds()
            )
            return False
        
        # Регистрируем новый запрос
        self._requests[user_id].append(now)
        return True


class UserDataStore:
    """Хранилище пользовательских данных с автоочисткой неактивных пользователей.
    
    Автоматически удаляет данные пользователей, неактивных более ttl_seconds.
    Предотвращает бесконечный рост памяти при долгой работе бота.
    """
    
    def __init__(self, ttl_seconds: int = USER_DATA_TTL_SECONDS):
        self._data: Dict[int, Dict[str, Any]] = {}
        self._last_access: Dict[int, float] = {}
        self.ttl = ttl_seconds
    
    def get(self, user_id: int, key: str, default: Any = None) -> Any:
        """Получает значение для пользователя."""
        self._cleanup()
        self._last_access[user_id] = time.time()
        return self._data.get(user_id, {}).get(key, default)
    
    def set(self, user_id: int, key: str, value: Any) -> None:
        """Устанавливает значение для пользователя."""
        self._cleanup()
        if user_id not in self._data:
            self._data[user_id] = {}
        self._data[user_id][key] = value
        self._last_access[user_id] = time.time()
    
    def _cleanup(self) -> None:
        """Удаляет неактивных пользователей (вызывается автоматически)."""
        now = time.time()
        expired = [
            uid for uid, last in self._last_access.items() 
            if now - last > self.ttl
        ]
        if expired:
            for uid in expired:
                self._data.pop(uid, None)
                self._last_access.pop(uid, None)
            logger.info("Cleaned up %d inactive users from memory", len(expired))

# Инициализация бота
if not config.bot_token:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
bot = telebot.TeleBot(config.bot_token)

# Хранилище пользовательских данных с автоочисткой
user_data_store = UserDataStore(ttl_seconds=USER_DATA_TTL_SECONDS)

# Rate limiter: 10 запросов в минуту на пользователя
rate_limiter = RateLimiter(max_requests=10, window_seconds=60)

# Глобальные кэши (статические данные, не растут бесконечно)
category_to_voices: Dict[str, List[Tuple[str, str]]] = {"male": [], "female": []}
voice_id_to_name: Dict[str, str] = {}
voice_id_to_lang: Dict[str, str] = {}
_voices_cache: Optional[Tuple[datetime, Any]] = None

def language_flag(lang: str) -> str:
    l = (lang or "").strip().lower()
    if l in ("ru", "russian", "русский"):  # русский
        return "🇷🇺"
    if "multi" in l or l in ("multi", "multilingual"):
        return "🌐"
    if l in ("en", "english", "английский"):
        return "🇬🇧"
    if l in ("us", "american"):
        return "🇺🇸"
    if l in ("de", "german", "немецкий"):
        return "🇩🇪"
    if l in ("fr", "french", "французский"):
        return "🇫🇷"
    if l in ("es", "spanish", "испанский"):
        return "🇪🇸"
    if l in ("it", "italian", "итальянский"):
        return "🇮🇹"
    if l in ("pt", "portuguese", "португальский"):
        return "🇵🇹"
    if l in ("tr", "turkish", "турецкий"):
        return "🇹🇷"
    if l in ("pl", "polish", "польский"):
        return "🇵🇱"
    return "🌐"

def mode_label_for(user_id: int) -> str:
    """Возвращает читаемую метку текущего режима псевдо-SSML для пользователя."""
    mode = user_data_store.get(user_id, "ssml_mode", "off")
    return MODE_LABELS.get(mode, mode)

def format_label_for(user_id: int) -> str:
    """Возвращает читаемую метку выбранного формата ответа."""
    fmt = user_data_store.get(user_id, "resp_format", BotConfig.FORMAT_BOTH)
    if fmt == BotConfig.FORMAT_OGG:
        return "OGG/Opus (голосовое)"
    if fmt == BotConfig.FORMAT_MP3:
        return "MP3 (файл для скачивания)"
    return "OGG + MP3"

def build_main_menu_kb() -> telebot.types.InlineKeyboardMarkup:
    """Создает клавиатуру главного меню."""
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("Мужские", callback_data=f"{BotConfig.CALLBACK_CATEGORY}male"),
        telebot.types.InlineKeyboardButton("Женские", callback_data=f"{BotConfig.CALLBACK_CATEGORY}female"),
    )
    kb.add(telebot.types.InlineKeyboardButton("Настройки", callback_data=BotConfig.CALLBACK_SETTINGS))
    return kb

def send_main_menu(chat_id: int, user_id: int) -> None:
    """Отправляет главное меню пользователю."""
    kb = build_main_menu_kb()
    
    # Получаем выбранный голос
    voice_id = user_data_store.get(user_id, "voice_id")
    voice_text = voice_id_to_name.get(voice_id, "Не выбран") if voice_id else "Не выбран"
    
    # Формируем текст сообщения
    message_text = (
        "📌 <b>Текущий режим:</b>\n"
        f"🎙 <b>Голос:</b> {voice_text}\n"
        f"⚙️ <b>Предобработка:</b> {mode_label_for(user_id)}\n\n"
        f"🎚 <b>Формат ответа:</b> {format_label_for(user_id)}\n\n"
        "💬 <i>Выберите голос для озвучки и режим предобработки.\n"
        "Введите текст для озвучки:</i>"
    )
    
    bot.send_message(
        chat_id,
        message_text,
        reply_markup=kb,
        parse_mode="HTML"
    )

def preprocess_text_if_needed(user_id: int, text: str) -> str:
    """Применяет псевдо-SSML предобработку текста через OpenAI (адаптация под ElevenLabs), если режим включен."""
    mode = user_data_store.get(user_id, "ssml_mode", "off")
    if mode == "off":
        return text
    try:
        style = "announcer" if mode == "announcer" else "conversational"
        processed = prepare_for_tts(text, style=style)
        if config.DEBUG:
            logger.debug("Pseudo-SSML mode=%s user_id=%s", mode, user_id)
            logger.debug("Original: %s", text)
            logger.debug("Processed: %s", processed)
        return processed
    except Exception as e:
        logger.error("Error in text preprocessing: %s", e)
        return text


class HTTPClientSingleton:
    """Singleton для переиспользования HTTP клиента с connection pooling."""
    _client: Optional[httpx.Client] = None
    
    @classmethod
    def get_client(cls) -> httpx.Client:
        """Возвращает singleton HTTP клиент с connection pooling."""
        if cls._client is None:
            cls._client = httpx.Client(
                timeout=httpx.Timeout(15.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
            )
        return cls._client


# Кэш для проверки квоты (обновляется раз в 5 минут)
_quota_cache: Optional[Tuple[float, Optional[int]]] = None
_QUOTA_CACHE_TTL = 300  # 5 минут


def check_quota_remaining_chars() -> Optional[int]:
    """Проверяет остаток квоты символов в ElevenLabs с кэшированием на 5 минут."""
    global _quota_cache
    
    # Проверяем кэш
    now = time.time()
    if _quota_cache is not None:
        cache_time, cached_value = _quota_cache
        if now - cache_time < _QUOTA_CACHE_TTL:
            if config.DEBUG:
                logger.debug("Using cached quota (age: %.1fs)", now - cache_time)
            return cached_value
    
    # Запрашиваем свежие данные
    try:
        api_key = config.elevenlabs_api_key
        if not api_key:
            return None
        
        client = HTTPClientSingleton.get_client()
        r = client.get(
            ELEVENLABS_USER_API_URL,
            headers={"xi-api-key": api_key}
        )
        
        if r.status_code != 200:
            return None
        
        data = r.json()
        limit_ = (data.get("subscription") or {}).get("character_limit")
        used_ = (data.get("subscription") or {}).get("character_count")
        
        if isinstance(limit_, int) and isinstance(used_, int):
            remaining = max(0, limit_ - used_)
            _quota_cache = (now, remaining)  # Кэшируем результат
            logger.debug("Quota check: %d chars remaining (cached for 5 min)", remaining)
            return remaining
        
        return None
    except Exception as e:
        logger.error("Error checking quota: %s", e)
        return None


def estimate_credits(text: str) -> int:
    """Оценивает необходимое количество кредитов для текста."""
    return max(1, int(len(text) / CREDITS_PER_CHARS))


def validate_user_message(message: telebot.types.Message) -> bool:
    """Проверяет, что сообщение от реального пользователя (не бота)."""
    if not message.from_user:
        logger.warning("Message without from_user: %s", message)
        return False
    if message.from_user.is_bot:
        logger.debug("Ignoring message from bot: %s", message.from_user.id)
        return False
    return True


def get_voices_cached() -> Any:
    """Возвращает список голосов с кэшированием на 1 час."""
    global _voices_cache
    now = datetime.now()
    
    if _voices_cache is not None:
        cache_time, cached_voices = _voices_cache
        if (now - cache_time).total_seconds() < VOICE_CACHE_TTL_SECONDS:
            logger.debug("Using cached voices (age: %.1fs)", (now - cache_time).total_seconds())
            return cached_voices
    
    # Кэш устарел или отсутствует, запрашиваем заново
    logger.info("Fetching fresh voices from ElevenLabs API")
    voices_response = get_all_voices()
    _voices_cache = (now, voices_response)
    return voices_response


@bot.message_handler(commands=['start'])
@measure_time
def send_welcome(message: telebot.types.Message) -> None:
    """Обработчик команды /start - загружает голоса и показывает главное меню."""
    try:
        voices_response = get_voices_cached()
        voice_list = voices_response.voices

        # Заполняем категории с последующей сортировкой по языку и имени
        category_to_voices["male"].clear()
        category_to_voices["female"].clear()
        temp_male = []  # (name, voice_id, language_label)
        temp_female = []

        for v in voice_list:
            labels = getattr(v, "labels", {}) if hasattr(v, "labels") else {}
            gender = labels.get("gender") or getattr(v, "gender", None)
            language_label = labels.get("language", "")
            name = getattr(v, "name", "Voice")
            vid = getattr(v, "voice_id", None)
            if not vid:
                continue
            voice_id_to_name[vid] = name
            voice_id_to_lang[vid] = language_label

            g = (gender or "").lower()
            if "female" in g:
                temp_female.append((name, vid, language_label))
            elif "male" in g:
                temp_male.append((name, vid, language_label))
            else:
                # неизвестный пол — отправим в мужские, чтобы не потерять
                temp_male.append((name, vid, language_label))

        def lang_priority(lang: str) -> int:
            l = (lang or "").lower()
            if l in ("ru", "russian", "русский"):
                return 0
            if "multi" in l:  # мультиязычные
                return 1
            return 2

        def sort_key(item):
            name, _vid, lang = item
            return (lang_priority(lang), name.lower())

        temp_male.sort(key=sort_key)
        temp_female.sort(key=sort_key)

        category_to_voices["male"] = [(n, vid) for (n, vid, _lang) in temp_male]
        category_to_voices["female"] = [(n, vid) for (n, vid, _lang) in temp_female]

        # Главное меню
        logger.info("Main menu opened for user_id=%s", message.from_user.id)
        send_main_menu(message.chat.id, message.from_user.id)
    except ApiError as e:
        logger.error("ElevenLabs ApiError during voice fetch: %s", e)
        bot.reply_to(
            message,
            "Не удалось получить список голосов (ошибка API).\n"
            "Проверьте API ключ ElevenLabs и доступ к сервису. Если вы находитесь в регионе с ограничениями, включите VPN и попробуйте снова командой /start.\n"
            f"Детали: {getattr(e, 'status_code', 'n/a')}"
        )
    except httpx.HTTPError as e:
        logger.error("Network error during voice fetch: %s", e)
        bot.reply_to(
            message,
            "Сетевая ошибка при получении голосов. Проверьте интернет/VPN и повторите командой /start."
        )
    except Exception as e:
        logger.exception("Unexpected error during voice fetch")
        bot.reply_to(message, "Произошла ошибка при загрузке голосов. Попробуйте снова.")

# Выбор категории
@bot.callback_query_handler(func=lambda c: c.data.startswith("cat:"))
def on_category(c: telebot.types.CallbackQuery) -> None:
    """Обработчик выбора категории голосов (мужские/женские)."""
    cat = c.data.split(":", 1)[1]
    voices = category_to_voices.get(cat, [])
    if not voices:
        bot.answer_callback_query(c.id, "Нет доступных голосов в этой категории")
        return

    # Клавиатура выбора голоса (inline)
    kb = telebot.types.InlineKeyboardMarkup()
    # по два в ряд
    row = []
    for name, vid in voices:
        flag = language_flag(voice_id_to_lang.get(vid, ""))
        label = f"{flag} {name}"
        row.append(telebot.types.InlineKeyboardButton(label, callback_data=f"{BotConfig.CALLBACK_VOICE}{vid}"))
        if len(row) == BotConfig.VOICE_BUTTONS_PER_ROW:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    
    # Кнопка возврата в главное меню
    kb.row(telebot.types.InlineKeyboardButton(BotConfig.BACK_BUTTON_TEXT, callback_data=BotConfig.CALLBACK_BACK))

    bot.edit_message_text(
        chat_id=c.message.chat.id,
        message_id=c.message.message_id,
        text=BotConfig.VOICE_SELECT_PROMPT,
        reply_markup=kb,
    )

# Выбор конкретного голоса
@bot.callback_query_handler(func=lambda c: c.data.startswith("voice:"))
def on_voice(c: telebot.types.CallbackQuery) -> None:
    """Обработчик выбора конкретного голоса."""
    vid = c.data.split(":", 1)[1]
    user_id = c.from_user.id
    user_data_store.set(user_id, "voice_id", vid)
    bot.answer_callback_query(c.id, "Голос выбран")
    send_main_menu(c.message.chat.id, user_id)

# Меню настроек
@bot.callback_query_handler(func=lambda c: c.data == "settings")
def on_settings(c: telebot.types.CallbackQuery) -> None:
    """Обработчик открытия меню настроек: псевдо-SSML и формат ответа."""
    user_id = c.from_user.id
    
    # Получаем текущие настройки
    voice_id = user_data_store.get(user_id, "voice_id")
    voice_text = voice_id_to_name.get(voice_id, "Не выбран") if voice_id else "Не выбран"
    mode_text = mode_label_for(user_id)
    format_text = format_label_for(user_id)
    
    # Формируем текст с текущими настройками
    settings_text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"📌 <b>Текущий режим:</b>\n"
        f"🎙 <b>Голос:</b> {voice_text}\n"
        f"⚙️ <b>Предобработка:</b> {mode_text}\n"
        f"🎚 <b>Формат ответа:</b> {format_text}\n\n"
        f"📝 <b>Режим предобработки (псевдо-SSML для ElevenLabs):</b>\n"
        f"🎭 <b>Формат ответа:</b>"
    )
    
    kb = telebot.types.InlineKeyboardMarkup()
    
    # Режим предобработки (псевдо-SSML для ElevenLabs)
    kb.row(
        telebot.types.InlineKeyboardButton("РАЗГОВОРНЫЙ СТИЛЬ", callback_data=f"{BotConfig.CALLBACK_SSML}conversational"),
        telebot.types.InlineKeyboardButton("ДИКТОРСКАЯ РЕЧЬ", callback_data=f"{BotConfig.CALLBACK_SSML}announcer")
    )
    kb.add(telebot.types.InlineKeyboardButton("Без предобработки", callback_data=f"{BotConfig.CALLBACK_SSML}off"))
    
    # Формат ответа
    kb.row(
        telebot.types.InlineKeyboardButton("🔊 OGG/Opus", callback_data=f"{BotConfig.CALLBACK_FORMAT}{BotConfig.FORMAT_OGG}"),
        telebot.types.InlineKeyboardButton("🎵 MP3", callback_data=f"{BotConfig.CALLBACK_FORMAT}{BotConfig.FORMAT_MP3}")
    )
    kb.add(telebot.types.InlineKeyboardButton("🔀 OGG + MP3", callback_data=f"{BotConfig.CALLBACK_FORMAT}{BotConfig.FORMAT_BOTH}"))
    
    # Кнопка возврата в главное меню
    kb.row(telebot.types.InlineKeyboardButton(BotConfig.BACK_BUTTON_TEXT, callback_data=BotConfig.CALLBACK_BACK))
    
    bot.edit_message_text(
        chat_id=c.message.chat.id,
        message_id=c.message.message_id,
        text=settings_text,
        reply_markup=kb,
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("ssml:"))
def on_ssml_mode(c: telebot.types.CallbackQuery) -> None:
    """Обработчик выбора режима псевдо-SSML предобработки."""
    mode = c.data.split(":", 1)[1]
    user_id = c.from_user.id
    user_data_store.set(user_id, "ssml_mode", mode)
    labels = {"off": "Выкл", "announcer": "ДИКТОРСКАЯ РЕЧЬ", "conversational": "РАЗГОВОРНЫЙ СТИЛЬ"}
    bot.answer_callback_query(c.id, f"Псевдо-SSML режим: {labels.get(mode, mode)}")
    send_main_menu(c.message.chat.id, user_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("fmt:"))
def on_format(c: telebot.types.CallbackQuery) -> None:
    """Обработчик выбора формата ответа."""
    fmt = c.data.split(":", 1)[1]
    user_id = c.from_user.id
    if fmt not in (BotConfig.FORMAT_OGG, BotConfig.FORMAT_MP3, BotConfig.FORMAT_BOTH):
        bot.answer_callback_query(c.id, "Неизвестный формат")
        return
    user_data_store.set(user_id, "resp_format", fmt)
    labels = {
        BotConfig.FORMAT_OGG: "OGG/Opus (голосовое)",
        BotConfig.FORMAT_MP3: "MP3 (файл)",
        BotConfig.FORMAT_BOTH: "OGG + MP3",
    }
    bot.answer_callback_query(c.id, f"Формат: {labels.get(fmt, fmt)}")
    send_main_menu(c.message.chat.id, user_id)

# Обработчик кнопки "Назад"
@bot.callback_query_handler(func=lambda c: c.data == "back_to_menu")
def on_back_to_menu(c: telebot.types.CallbackQuery) -> None:
    """Обработчик возврата в главное меню."""
    user_id = c.from_user.id
    bot.answer_callback_query(c.id)
    # Удаляем старое сообщение и отправляем новое главное меню
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except ApiTelegramException as e:
        # Сообщение уже удалено или недоступно
        if config.DEBUG:
            logger.debug("Failed to delete message: %s", e)
    except Exception as e:
        logger.warning("Unexpected error deleting message: %s", e)
    send_main_menu(c.message.chat.id, user_id)


@bot.message_handler(func=lambda msg: True)
@measure_time
def generate_voice(message: telebot.types.Message) -> None:
    """Основной обработчик текстовых сообщений - генерирует аудио."""
    # Валидация пользователя
    if not validate_user_message(message):
        return
    
    user_id = message.from_user.id
    
    # Проверка rate limiting
    if not rate_limiter.is_allowed(user_id):
        bot.reply_to(
            message,
            "⚠️ Слишком много запросов. Подождите немного и попробуйте снова.\n"
            "(Лимит: 10 запросов в минуту)"
        )
        return
    
    voice_id = user_data_store.get(user_id, "voice_id")
    if not voice_id:
        bot.reply_to(message, "Сначала выберите голос командой /start")
        return

    try:
        text = message.text
        if config.DEBUG:
            logger.debug("Incoming text from user_id=%s: %s", user_id, text)

        # Ограничиваем длину текста
        if len(text) > config.MAX_TTS_CHARS:
            bot.reply_to(message, f"Текст слишком длинный (>{config.MAX_TTS_CHARS} символов). Сократите, пожалуйста.")
            return

        # Генерируем MP3 и OPUS (OGG контейнер)
        processed_text = preprocess_text_if_needed(user_id, text)

        # Проверка квоты по символам до обращения к ElevenLabs
        remaining = check_quota_remaining_chars()
        needed_chars = len(processed_text)
        if remaining is not None and remaining < needed_chars:
            need_credits = estimate_credits(processed_text)
            have_credits = estimate_credits("x" * remaining) if remaining >= 0 else 0
            bot.reply_to(
                message,
                (
                    "Недостаточно квоты ElevenLabs.\n"
                    f"Осталось символов: {remaining}, требуется: {needed_chars}.\n"
                    f"Оценка кредитов: нужно ~{need_credits}, есть ~{have_credits}.\n"
                    "Сократите текст или пополните баланс и попробуйте снова."
                ),
            )
            return
        # Генерируем два разных формата
        voice_name = voice_id_to_name.get(voice_id, "Voice")
        mode = user_data_store.get(user_id, "ssml_mode", "off")
        
        # Формируем имя файла (имя голоса уже содержит префикс, просто добавляем режим)
        base_name = f"{voice_name} - {MODE_LABELS.get(mode, mode)}"

        # Отправка согласно выбранному формату
        fmt = user_data_store.get(user_id, "resp_format", BotConfig.FORMAT_BOTH)
        send_ogg = fmt in (BotConfig.FORMAT_OGG, BotConfig.FORMAT_BOTH)
        send_mp3 = fmt in (BotConfig.FORMAT_MP3, BotConfig.FORMAT_BOTH)

        opus_bytes = None
        mp3_bytes = None
        
        if send_ogg:
            # Opus 48k для корректной визуализации голосового сообщения с волной в Telegram
            opus_bytes = generate_audio(processed_text, voice_id, output_format="opus_48000_64")
            voice_io = io.BytesIO(opus_bytes)
            voice_io.name = "voice.ogg"  # важно для Telegram!
            voice_io.seek(0)
            bot.send_voice(chat_id=user_id, voice=voice_io)

        if send_mp3:
            # MP3 как именованный файл для скачивания
            mp3_bytes = generate_audio(processed_text, voice_id, output_format="mp3_44100_128")
            audio_io = io.BytesIO(mp3_bytes)
            audio_io.seek(0)
            bot.send_audio(
                chat_id=user_id, 
                audio=(base_name + ".mp3", audio_io),
                title=base_name,
                performer="ZeroTTS"
            )
    except ApiError as e:
        code = getattr(e, 'status_code', None)
        body = getattr(e, 'body', {}) or {}
        detail = body.get('detail') if isinstance(body, dict) else None
        status = (detail.get('status') if isinstance(detail, dict) else None) or ""
        msg = (detail.get('message') if isinstance(detail, dict) else None) or ""
        
        if code == 401 and status == 'quota_exceeded':
            # Это не баг, а информирование о нехватке кредитов
            remaining_cred = None
            required_cred = None
            try:
                nums = list(map(int, re.findall(r"(\d+)", msg)))
                if len(nums) >= 2:
                    remaining_cred, required_cred = nums[-2], nums[-1]
            except Exception:
                pass
            if config.DEBUG:
                logger.info("ElevenLabs quota_exceeded: %s", msg)
            
            if remaining_cred is not None and required_cred is not None:
                bot.reply_to(
                    message,
                    (
                        "Недостаточно кредитов ElevenLabs.\n"
                        f"Осталось: {remaining_cred}, требуется: {required_cred}.\n"
                        "Сократите текст или пополните баланс и попробуйте снова."
                    ),
                )
            else:
                bot.reply_to(
                    message,
                    "Недостаточно кредитов ElevenLabs. Сократите текст или пополните баланс и попробуйте снова.",
                )
        else:
            # Прочие случаи ApiError
            logger.error("ElevenLabs ApiError code=%s body=%s", code, body)
            hint = "Проверьте VPN/ключ и повторите" if code in (401, 403) else "Повторите попытку позже"
            bot.reply_to(message, f"Не удалось сгенерировать аудио ({code}). {hint}")
    except httpx.HTTPError as e:
        logger.error("Network error during audio generation: %s", e)
        bot.reply_to(message, "Сетевая ошибка при генерации. Проверьте интернет/VPN и попробуйте снова.")
    except Exception as e:
        logger.exception("Unexpected error during audio generation")
        bot.reply_to(message, "Не удалось сгенерировать аудио. Попробуйте снова или смените голос.")


if __name__ == '__main__':
    logger.info("Starting ZeroTTS bot...")
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.error("Polling crashed, restarting in 3s: %s", e)
            time.sleep(3)
            continue
