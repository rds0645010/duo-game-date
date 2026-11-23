"""
Главная точка входа DUO Game & Media Bot:
инициализирует конфигурацию, БД (db.py), пул HTTP-соединений (http_pool.py),
подключает модульные роутеры (routers/) и запускает Polling/Webhook + WebApp API.
"""

import os
import re
import sys
import logging
import asyncio
import html
import time
from typing import Optional
import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import InlineKeyboardMarkup, FSInputFile

from downloader import (
    detect_platform,
    detect_content_info,
    extract_url,
    extract_all_media_urls,
    download_video,
    parse_tiktok_tikwm,
    FFMPEG_PATH,
    DOWNLOAD_DIR,
    extract_direct_video_info,
    parse_instagram_embed,
    is_valid_mp4_file,
)
import warp_proxy
import music_service
import db
import http_pool
import routers

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 📋 ДИАГНОСТИКА И ЛОГИРОВАНИЕ ОШИБОК
recent_errors = []


def record_error(source: str, error: Exception, details: str = ""):
    import traceback
    recent_errors.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "error": str(error),
        "type": type(error).__name__,
        "details": details,
        "traceback": traceback.format_exc(),
    })
    if len(recent_errors) > 20:
        recent_errors.pop(0)


# ==========================================
# ⚙️ КОНФИГУРАЦИЯ И НЕЗАВИСИМЫЕ СЕМАФОРЫ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rds0645010.github.io/duo-game-date/").strip()
ORGANIZER_CHAT_ID = os.getenv("ORGANIZER_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", 8080))
DB_PATH = "users.db"

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS = set([x.strip() for x in ADMIN_ID_RAW.split(",") if x.strip()])
DEBUG_API_KEY = os.getenv("DEBUG_API_KEY", "").strip()

TIKTOK_SEMAPHORE = asyncio.Semaphore(5)
tiktok_waiting_count = 0
tiktok_lock = asyncio.Lock()

INSTAGRAM_SEMAPHORE = asyncio.Semaphore(2)
instagram_waiting_count = 0
instagram_lock = asyncio.Lock()

try:
    MAX_YOUTUBE_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1") or "1"))
except ValueError:
    MAX_YOUTUBE_DOWNLOADS = 1
YOUTUBE_SEMAPHORE = asyncio.Semaphore(MAX_YOUTUBE_DOWNLOADS)
youtube_waiting_count = 0
youtube_lock = asyncio.Lock()

MUSIC_SEMAPHORE = asyncio.Semaphore(3)
music_waiting_count = 0
music_lock = asyncio.Lock()

_slideshow_cache = {}
_stream_files = {}   # token -> local_file_path
_stream_locks = {}   # token -> asyncio.Lock
_precache_semaphore = asyncio.Semaphore(1)


# ==========================================
# 🗄️ ПРОСЛОЙКА БАЗЫ ДАННЫХ (ДЕЛЕГИРОВАНИЕ В db.py)
# ==========================================
def init_db():
    db.init_db(DB_PATH)


def get_bot_setting(key: str, default: str = "") -> str:
    return db.get_bot_setting(key, default=default, db_path=DB_PATH)


def set_bot_setting(key: str, value: str):
    db.set_bot_setting(key, value, db_path=DB_PATH)


def save_slideshow_session_db(sid: str, data: dict):
    db.save_slideshow_session_db(sid, data, db_path=DB_PATH)


def get_slideshow_session_db(sid: str) -> Optional[dict]:
    data = _slideshow_cache.get(sid)
    if data:
        return data
    restored = db.load_slideshow_session_db(sid, db_path=DB_PATH)
    if restored:
        _slideshow_cache[sid] = restored
    return restored


def get_canonical_media_key(url: str) -> str:
    return db.get_canonical_media_key(url)


def record_user_download(user_id: int, url: str, file_id: str, media_type: str, title: str, platform: str):
    db.record_user_download(user_id, url, file_id, media_type, title, platform, db_path=DB_PATH)


def get_user_recent_downloads(user_id: int, limit: int = 15):
    return db.get_user_recent_downloads(user_id, limit=limit, db_path=DB_PATH)


def save_dl_token(url: str, file_path: str = None) -> str:
    return db.save_dl_token(url, file_path=file_path, db_path=DB_PATH)


def update_dl_token_file(token: str, file_path: str):
    db.update_dl_token_file(token, file_path, db_path=DB_PATH)


def get_dl_token_url(token: str):
    url, _ = db.get_dl_token_info(token, db_path=DB_PATH)
    return url


def get_dl_token_info(token: str):
    return db.get_dl_token_info(token, db_path=DB_PATH)


def get_user_inline_caption(user_id: int) -> bool:
    return db.get_user_inline_caption(user_id, db_path=DB_PATH)


def set_user_inline_caption(user_id: int, enabled: bool):
    db.set_user_inline_caption(user_id, enabled, db_path=DB_PATH)


def get_user_save_to_pm(user_id: int) -> bool:
    return db.get_user_save_to_pm(user_id, db_path=DB_PATH)


def set_user_save_to_pm(user_id: int, enabled: bool):
    db.set_user_save_to_pm(user_id, enabled, db_path=DB_PATH)


def get_cached_media(url: str):
    return db.get_cached_media(url, db_path=DB_PATH)


def get_cached_video(url: str):
    return db.get_cached_media(url, db_path=DB_PATH)


def is_instagram_reel_url(url: str) -> bool:
    return bool(re.search(r'(?:instagram\.com|instagr\.am)/(?:[A-Za-z0-9._]+/)?(?:reel|reels|tv|share/reel)/', url or "", re.IGNORECASE))


def save_cached_media(url: str, file_id: str, media_type: str, title: str, platform: str, author: str = "", music: str = None, music_info: dict = None):
    if media_type == "photo" and is_instagram_reel_url(url):
        logging.warning(f"Refusing to cache photo for Instagram Reel URL: {url}")
        return
    db.save_cached_media(url, file_id, media_type, title, platform, author, music=music, music_info=music_info, db_path=DB_PATH)


def delete_cached_media(url: str):
    db.delete_cached_media(url, db_path=DB_PATH)


def drop_poisoned_media_cache(url: str, cached: dict = None) -> bool:
    """Сбрасывает ошибочный photo-кэш для видеороликов (TikTok, Instagram Reels, Shorts)."""
    if not cached or not cached.get("file_id") or not url:
        return False
    if cached.get("media_type") == "photo":
        u_lower = url.lower()
        is_video_url = (
            "/video/" in u_lower
            or "/reel/" in u_lower
            or "/reels/" in u_lower
            or "/tv/" in u_lower
            or "/shorts/" in u_lower
            or "watch?v=" in u_lower
            or "youtu.be/" in u_lower
            or is_instagram_reel_url(url)
        )
        if is_video_url:
            logging.info(f"🧹 Сброс отравленного фото-кэша для видео: {url}")
            delete_cached_media(url)
            return True
    return False


drop_poisoned_instagram_photo_cache = drop_poisoned_media_cache


def save_cached_video(url: str, file_id: str, title: str, platform: str, author: str = ""):
    save_cached_media(url, file_id, "video", title, platform, author)


def get_fallback_upload_chat():
    if ADMIN_IDS:
        for aid in ADMIN_IDS:
            try:
                return int(aid)
            except Exception:
                pass
    uid = db.get_any_known_user_id(db_path=DB_PATH)
    return int(uid) if uid else None


def save_user(user_id: int, username: str, first_name: str, photo_url: str = None, gender: str = None):
    db.save_user(user_id, username, first_name, photo_url=photo_url, gender=gender, db_path=DB_PATH)


def find_user_by_username(username: str):
    return db.find_user_by_username(username, db_path=DB_PATH)


def create_invite_record(sender_id, sender_name, recipient_username, place, day, time_str, vibe, duration, note):
    return db.create_invite_record(sender_id, sender_name, recipient_username, place, day, time_str, vibe, duration, note, db_path=DB_PATH)


def get_invite_record(invite_id: int):
    return db.get_invite_record(invite_id, db_path=DB_PATH)


# ==========================================
# 📤 ОБЩИЕ ХЕЛПЕРЫ TELEGRAM И ПОДПИСЕЙ
# ==========================================
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0") or "0")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID", "").strip()

init_db()

bot_session = AiohttpSession(timeout=180.0)
bot = Bot(token=BOT_TOKEN, session=bot_session) if (BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE") else None
dp = Dispatcher()

IS_ON_RENDER = bool(os.getenv("RENDER") or os.getenv("RENDER_SERVICE_ID") or os.getenv("RENDER_INSTANCE_ID"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip() or "https://duo-game-date-bot.onrender.com"
if not RENDER_EXTERNAL_URL.startswith(("http://", "https://")):
    RENDER_EXTERNAL_URL = f"https://{RENDER_EXTERNAL_URL}"
RENDER_EXTERNAL_URL = RENDER_EXTERNAL_URL.rstrip("/")

USE_WEBHOOK = os.getenv("USE_WEBHOOK", "").strip().lower() in ("1", "true", "yes")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or None


def get_app_url():
    base = WEBAPP_URL
    if RENDER_EXTERNAL_URL:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}srv={RENDER_EXTERNAL_URL}"
    return base


_bg_tasks = set()


def track_background_task(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def get_storage_channel_id() -> Optional[int]:
    raw = (STORAGE_CHANNEL_ID or os.getenv("STORAGE_CHANNEL_ID", "") or get_bot_setting("storage_channel_id", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _delete_message_reliably(
    chat_id: int,
    message_id: int,
    delay_seconds: float = 0.0,
    max_attempts: int = 4,
) -> bool:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    for attempt in range(1, max_attempts + 1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except Exception as e:
            err_l = str(e).lower()
            if "message to delete not found" in err_l or "message can't be deleted" in err_l:
                return True
            if hasattr(e, "retry_after"):
                await asyncio.sleep(float(getattr(e, "retry_after")) + 0.3)
                continue
            logging.warning(
                f"delete_message failed (attempt {attempt}/{max_attempts}) chat={chat_id} msg={message_id}: {e}"
            )
            await asyncio.sleep(0.5 * attempt)
    return False


async def _delayed_delete_message(chat_id: int, message_id: int, delay_seconds: int = 60):
    await _delete_message_reliably(chat_id, message_id, delay_seconds=delay_seconds)


async def upload_media_to_telegram(
    file_path: str,
    is_photo: bool = False,
    is_audio: bool = False,
    target_user_id: int = None,
    title: str = "",
    platform: str = "",
    performer: str = "",
    duration: int = 0,
    force_ephemeral: bool = False,
):
    """Загружает файл в Telegram для получения постоянного CDN file_id."""
    if not file_path or not os.path.exists(file_path):
        return None

    caption_text = None
    if target_user_id:
        t_clean = html.escape((title or "Медиа")[:150])
        p_clean = html.escape((platform or "Медиа").capitalize())
        caption_text = (
            f"🎬 <b>{t_clean}</b>\n\n"
            f"📱 Источник: {p_clean}\n"
            f"✨ <i>Сохранено при отправке через @duo_play_date_bot</i>"
        )

    should_save_in_pm = (not force_ephemeral) and bool(target_user_id and get_user_save_to_pm(target_user_id))
    storage_id = get_storage_channel_id()

    owner_id = None
    if OWNER_USER_ID:
        owner_id = OWNER_USER_ID

    fb_chat = get_fallback_upload_chat()

    candidates = []
    seen_chats = set()

    def _add_candidate(cid: Optional[int], keep: bool):
        if cid is None or cid in seen_chats:
            return
        seen_chats.add(cid)
        candidates.append((cid, keep))

    if should_save_in_pm:
        _add_candidate(target_user_id, True)
        _add_candidate(storage_id, True)
        _add_candidate(owner_id, False)
        _add_candidate(fb_chat, False)
    else:
        _add_candidate(storage_id, True)
        _add_candidate(target_user_id, False)
        _add_candidate(owner_id, False)
        _add_candidate(fb_chat, False)

    for chat_id, keep_message in candidates:
        try:
            use_caption = caption_text if (keep_message and chat_id == target_user_id and should_save_in_pm) else None
            parse_mode = "HTML" if use_caption else None
            sent = None
            f_id = None

            if is_audio:
                sent = await bot.send_audio(
                    chat_id=chat_id,
                    audio=FSInputFile(file_path),
                    performer=performer,
                    title=title,
                    duration=duration,
                    caption=use_caption,
                    parse_mode=parse_mode,
                    disable_notification=True,
                )
                if sent and sent.audio:
                    f_id = sent.audio.file_id
            elif is_photo:
                sent = await bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(file_path),
                    caption=use_caption,
                    parse_mode=parse_mode,
                    disable_notification=True,
                )
                if sent and sent.photo:
                    f_id = sent.photo[-1].file_id
            else:
                sent = await bot.send_video(
                    chat_id=chat_id,
                    video=FSInputFile(file_path),
                    caption=use_caption,
                    parse_mode=parse_mode,
                    supports_streaming=True,
                    disable_notification=True,
                )
                if sent and sent.video:
                    f_id = sent.video.file_id

            if f_id:
                if not keep_message and chat_id > 0:
                    deleted = await _delete_message_reliably(
                        chat_id, sent.message_id, delay_seconds=0.0, max_attempts=3
                    )
                    if not deleted:
                        track_background_task(
                            _delete_message_reliably(chat_id, sent.message_id, delay_seconds=3.0, max_attempts=3)
                        )
                return f_id
        except Exception as u_err:
            logging.info(f"Upload to chat {chat_id} failed: {u_err}")

    logging.warning(
        f"upload_media_to_telegram exhausted all upload chats (target={target_user_id}, storage={storage_id})"
    )
    return None


async def safe_edit_status(
    msg: Optional[types.Message] = None,
    text: str = "",
    inline_message_id: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    if not msg and not inline_message_id:
        return
    try:
        if inline_message_id:
            await bot.edit_message_text(
                inline_message_id=inline_message_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
            )
        elif msg:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logging.debug(f"Status edit ignored: {e}")


def create_progress_reporter(
    status_msg: Optional[types.Message] = None,
    content_info_or_name=None,
    inline_message_id: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    throttle_seconds: float = 1.0,
):
    last_update = 0.0
    last_rendered_pct = -1.0
    last_rendered_text = ""

    if isinstance(content_info_or_name, dict):
        label = content_info_or_name.get("label") or "медиа"
        icon = content_info_or_name.get("icon") or "⚡"
    else:
        label = str(content_info_or_name or "медиа")
        icon = "⚡"

    async def update_progress(downloaded: int, total: int, speed: float = 0.0, eta: float = 0.0, is_slide: bool = False, phase: str = ""):
        nonlocal last_update, last_rendered_pct, last_rendered_text
        if not status_msg and not inline_message_id:
            return
        now = time.time()

        if downloaded == -1:
            if now - last_update >= 0.8:
                last_update = now
                text = "📤 <b>Отправляю в Telegram...</b>\n<code>[████████████] 100%</code>"
                if text != last_rendered_text:
                    last_rendered_text = text
                    await safe_edit_status(msg=status_msg, text=text, inline_message_id=inline_message_id, reply_markup=reply_markup)
            return

        if phase in ("converting", "processing"):
            if now - last_update >= 0.5:
                last_update = now
                text = "🎵 <b>Конвертирую трек и упаковываю обложку...</b>\n<code>[██████████░░] 85%</code>\n✨ Почти готово к отправке..."
                if text != last_rendered_text:
                    last_rendered_text = text
                    await safe_edit_status(msg=status_msg, text=text, inline_message_id=inline_message_id, reply_markup=reply_markup)
            return

        if phase and total <= 0:
            if now - last_update >= throttle_seconds:
                last_update = now
                text = f"{icon} <b>Загружаю {label}...</b>\n<code>[███░░░░░░░░░] 25%</code>\nℹ️ {phase}"
                if text != last_rendered_text:
                    last_rendered_text = text
                    await safe_edit_status(msg=status_msg, text=text, inline_message_id=inline_message_id, reply_markup=reply_markup)
            return

        if is_slide or (0 < total <= 50 and downloaded <= total):
            pct = min(100.0, max(0.0, (downloaded / total) * 100.0))
            if (now - last_update < throttle_seconds) and abs(pct - last_rendered_pct) < 15 and pct < 100:
                return
            last_update, last_rendered_pct = now, pct
            filled = int(round(12 * pct / 100.0))
            bar = "█" * filled + "░" * (12 - filled)
            text = f"📸 <b>Скачиваю фото-слайды ({downloaded} из {total})...</b>\n<code>[{bar}] {int(pct)}%</code>\n✨ Готовлю слайдшоу для просмотра..."
            if text != last_rendered_text:
                last_rendered_text = text
                await safe_edit_status(msg=status_msg, text=text, inline_message_id=inline_message_id, reply_markup=reply_markup)
            return

        if total > 0:
            pct = min(100.0, max(0.0, (downloaded / total) * 100.0))
            if (now - last_update < throttle_seconds) and abs(pct - last_rendered_pct) < 10 and pct < 100:
                return
            last_update, last_rendered_pct = now, pct
            filled = int(round(12 * pct / 100.0))
            bar = "█" * filled + "░" * (12 - filled)
            cur_mb, tot_mb = downloaded / (1024 * 1024), total / (1024 * 1024)
            spd_str = f" • {speed / (1024 * 1024):.1f} МБ/с" if speed > 0 else ""
            eta_str = f"\n⏱ Осталось: ~{max(1, int(round(eta)))} сек" if eta > 0 else ""
            text = f"{icon} <b>Скачиваю {label}...</b>\n<code>[{bar}] {int(pct)}%</code>\n📊 {cur_mb:.1f} из {tot_mb:.1f} МБ{spd_str}{eta_str}"
            if text != last_rendered_text:
                last_rendered_text = text
                await safe_edit_status(msg=status_msg, text=text, inline_message_id=inline_message_id, reply_markup=reply_markup)
            return

        if total <= 0 and downloaded > 0 and now - last_update >= throttle_seconds:
            last_update = now
            cur_mb = downloaded / (1024 * 1024)
            spd_str = f" • {speed / (1024 * 1024):.1f} МБ/с" if speed > 0 else ""
            text = f"{icon} <b>Скачиваю {label}...</b>\n📊 Загружено: {cur_mb:.1f} МБ{spd_str}"
            if text != last_rendered_text:
                last_rendered_text = text
                await safe_edit_status(msg=status_msg, text=text, inline_message_id=inline_message_id, reply_markup=reply_markup)

    return update_progress


def format_count(n) -> str:
    try:
        val = int(n or 0)
    except (ValueError, TypeError):
        return ""
    if val <= 0:
        return ""
    if val < 1000:
        return str(val)
    if val < 1_000_000:
        k_val = val / 1000.0
        return f"{int(k_val)}K" if k_val >= 100 else f"{k_val:.1f}K".replace(".0K", "K")
    m_val = val / 1_000_000.0
    return f"{int(m_val)}M" if m_val >= 100 else f"{m_val:.1f}M".replace(".0M", "M")


def format_description_line(title: str, is_photo: bool = False, is_audio: bool = False) -> Optional[str]:
    title_raw = (title or "").strip()
    title_no_tags = re.sub(r'#[^\s#]+', '', title_raw)
    title_no_tags = re.sub(r'\s+([,.:!?])', r'\1', title_no_tags)
    title_clean = re.sub(r'\s+', ' ', title_no_tags).strip()
    if not title_clean:
        return None
    if title_clean.lower() in (
        "медиа", "видео", "фото", "tiktok video", "reels", "shorts",
        "instagram публикация", "публикация", "tiktok фото", "instagram reel",
        "instagram", "tiktok", "youtube", "media", "audio", "sound",
    ):
        return None
    if len(title_clean) > 280:
        title_clean = title_clean[:280].rstrip() + "..."
    icon = "🎵" if is_audio else ("📸" if is_photo else "🎬")
    label = "Трек" if is_audio else "Описание"
    return f"{icon} <b>{label}:</b> <tg-spoiler>{html.escape(title_clean)}</tg-spoiler>"


def format_music_line(music_info: dict = None, music: str = None, post_title: str = None) -> Optional[str]:
    m_title = ""
    m_author = ""
    if isinstance(music_info, dict):
        m_title = (music_info.get("title") or music_info.get("song") or "").strip()
        m_author = (music_info.get("author") or music_info.get("artist") or "").strip()
    elif isinstance(music, str) and not music.startswith("http"):
        m_title = music.strip()

    if not m_title:
        return None
    m_lower = m_title.lower()
    if m_lower in (
        "original sound", "оригинальный звук", "original audio", "оригинальное аудио",
        "tiktok", "instagram", "video", "shorts", "audio", "sound",
    ):
        return None

    if post_title:
        c_m = re.sub(r'#[^\s#]+', '', m_title).strip().lower()
        c_p = re.sub(r'#[^\s#]+', '', post_title).strip().lower()
        if c_m and (c_m == c_p or c_m in c_p or c_p in c_m):
            return None

    for orig_prefix in ("original sound -", "оригинальный звук -", "original audio -", "оригинальное аудио -"):
        if m_lower.startswith(orig_prefix):
            clean_orig = m_title[len(orig_prefix):].strip()
            return f"🎵 <b>Звук:</b> {html.escape(clean_orig)}" if clean_orig else None

    m_title_esc = html.escape(m_title)
    if m_author and m_author.lower() not in ("tiktok", "instagram", "artist", "author", "creator", "unknown"):
        if m_author.lower() in m_lower:
            return f"🎵 <b>Музыка:</b> {m_title_esc}"
        return f"🎵 <b>Музыка:</b> {m_title_esc} — {html.escape(m_author)}"
    return f"🎵 <b>Музыка:</b> {m_title_esc}"


def _safe_remove(path: Optional[str]):
    """Удаляет временный файл после успешной отправки в Telegram."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# ==========================================
# 🧩 РЕГИСТРАЦИЯ МОДУЛЬНЫХ РОУТЕРОВ И РЕЭКСПОРТЫ
# ==========================================
routers.set_app_module(sys.modules[__name__])
routers.setup_all_routers(dp)

# Реэкспорт функций модулей для обратной совместимости (тесты и прямые импорты)
from routers.admin import (  # noqa: E402
    is_bot_admin,
    admin_denied_text,
    verify_telegram_init_data,
    debug_api_allowed as _debug_api_allowed,
    handle_cookies_command,
    handle_del_cookies_command,
    handle_cookies_document,
)
from routers.slideshow import (  # noqa: E402
    expire_slideshow_session as _expire_slideshow_session,
    preupload_slideshow_file_ids as _preupload_slideshow_file_ids,
    make_slide_caption,
    build_slideshow_keyboard,
    handle_slide_switch_callback,
    handle_refresh_slide_callback,
    handle_slide_collapse_callback,
    handle_slide_expand_callback,
    handle_slide_stat_callback,
    handle_slide_music_callback,
    handle_slide_album_all_callback,
    handle_slide_info_callback,
    handle_slide_noop_callback,
)
from routers.music import (  # noqa: E402
    handle_btn_music_search,
    cmd_music_search,
    handle_music_download_callback,
    handle_cancel_music_callback,
)
from routers.inline import (  # noqa: E402
    make_inline_caption,
    execute_inline_delivery,
    inline_query_handler,
    handle_chosen_inline_result,
    handle_get_vid_callback,
    handle_noop_callback,
    handle_retry_callback,
    _active_inline_jobs,
)
from routers.downloads import (  # noqa: E402
    format_friendly_error,
    process_video_download,
    handle_text_messages,
    CHATTER_WORDS,
)
from routers.webapp_api import (  # noqa: E402
    cmd_start,
    get_settings_keyboard,
    get_settings_text,
    cmd_settings,
    handle_open_settings,
    callback_toggle_caption,
    callback_toggle_pm,
    handle_accept,
    handle_later,
    handle_decline,
    expire_stream_file as _expire_stream_file,
    pre_cache_video_to_disk,
    handle_stream_video,
    handle_health,
    handle_api_warp,
    handle_api_check_user,
    handle_api_send_invite,
    handle_api_logs,
    handle_api_test_dl,
    create_web_app,
)


# ==========================================
# 🔄 ФОНОВЫЕ СЕРВИСЫ И ТОЧКА ВХОДА
# ==========================================
async def warm_up_warp():
    status = warp_proxy.warp.status()
    if not status["enabled"]:
        logging.info("WARP отключён (WARP_ENABLED=false)")
        return
    if not status["supported"]:
        logging.info("WARP не поддерживается на этой платформе — YouTube пойдёт напрямую")
        return
    try:
        ok = await warp_proxy.warp.ensure_ready()
        if ok:
            st = warp_proxy.warp.status()
            logging.info(
                f"🌐 YouTube через WARP готов: IP {st['egress_ip']} ({st['egress_colo']}), "
                f"RAM ~{st['rss_mb']} МБ"
            )
        else:
            logging.warning(f"WARP не поднялся: {warp_proxy.warp.status()['last_error']}")
    except Exception as e:
        logging.warning(f"WARP warm-up error: {e}")


async def storage_janitor(interval_seconds: int = 900, max_age_seconds: int = 2 * 60 * 60):
    """Фоновая уборка temp_downloads, RAM-кэша слайдшоу и старых записей SQLite."""
    await asyncio.sleep(120)
    while True:
        try:
            now = time.time()
            stale_sids = [
                sid for sid, d in list(_slideshow_cache.items())
                if now - float((d or {}).get("created_at") or 0) > max_age_seconds
            ]
            for sid in stale_sids:
                _slideshow_cache.pop(sid, None)

            removed = 0
            freed = 0
            active_stream = set(_stream_files.values())
            for name in os.listdir(DOWNLOAD_DIR):
                path = os.path.join(DOWNLOAD_DIR, name)
                try:
                    if not os.path.isfile(path) or path in active_stream:
                        continue
                    if now - os.path.getmtime(path) > max_age_seconds:
                        freed += os.path.getsize(path)
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass

            pruned = await db.run_db(db.cleanup_old_db_records, DB_PATH)
            pruned_total = sum(pruned.values()) if isinstance(pruned, dict) else 0
            if removed or stale_sids or pruned_total:
                logging.info(
                    f"🧹 Janitor: удалено файлов {removed} ({freed / 1048576:.1f} МБ), "
                    f"выгружено слайдшоу из RAM: {len(stale_sids)}, очищено в БД: {pruned}"
                )
        except Exception as e:
            logging.warning(f"storage_janitor error: {e}")
        await asyncio.sleep(interval_seconds)


async def keep_alive_ping():
    """Каждые 9 минут отправляет HTTP GET на /health через общий пул соединений."""
    await asyncio.sleep(45)
    target_url = f"{RENDER_EXTERNAL_URL}/health"
    logging.info(f"🔄 Фоновый анти-сон запущен: цель {target_url}")
    while True:
        try:
            session = await http_pool.get_http_session()
            async with session.get(target_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                logging.info(f"⏰ Anti-sleep keepalive: HTTP {resp.status}")
        except Exception as e:
            logging.debug(f"Anti-sleep ping error: {e}")
        await asyncio.sleep(540)


async def main():
    from aiohttp import web

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ ВНИМАНИЕ: BOT_TOKEN не указан в .env файле!")
        print("Пожалуйста, получи токен у @BotFather и пропиши BOT_TOKEN=твой_токен")

    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"🌐 HTTP API запущен на порту {PORT}")

    keepalive_task = asyncio.create_task(keep_alive_ping())
    janitor_task = asyncio.create_task(storage_janitor())
    asyncio.create_task(warm_up_warp())

    try:
        if bot:
            try:
                await bot.set_my_commands([
                    types.BotCommand(command="start", description="Главное меню и игры в DUO"),
                    types.BotCommand(command="music", description="Поиск и скачивание треков (Spotify/MP3)"),
                    types.BotCommand(command="settings", description="Настройки подписи медиа"),
                ])
            except Exception as cmd_err:
                logging.debug(f"set_my_commands notice: {cmd_err}")

            if USE_WEBHOOK:
                webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
                logging.info(f"🌐 Режим WEBHOOK активирован. Целевой URL: {webhook_url}")
                try:
                    info = await bot.get_webhook_info()
                    if info.url != webhook_url:
                        await bot.set_webhook(
                            url=webhook_url,
                            allowed_updates=dp.resolve_used_update_types(),
                            drop_pending_updates=False,
                            secret_token=WEBHOOK_SECRET,
                        )
                        logging.info(f"✅ Webhook успешно установлен: {webhook_url}")
                    else:
                        logging.info(f"✅ Webhook уже активен: {webhook_url}")
                except Exception as e:
                    logging.error(f"❌ Не удалось установить Webhook: {e}")
                    record_error("webhook_setup", e)

                logging.info("🤖 Бот слушает Telegram через Webhook (24/7)...")
                while True:
                    await asyncio.sleep(3600)
            else:
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                    logging.info("🧹 Webhook снят для работы в режиме Polling")
                except Exception as e:
                    logging.debug(f"delete_webhook error: {e}")
                logging.info("🤖 Бот запущен в режиме Polling (локально)...")
                await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        else:
            logging.warning("⚠️ Бот не запущен, так как нет BOT_TOKEN. HTTP сервер работает.")
            while True:
                await asyncio.sleep(3600)
    finally:
        for t in (keepalive_task, janitor_task):
            t.cancel()
        await http_pool.close_http_session()
        await runner.cleanup()
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
