import os
import re
import json
import sqlite3
import logging
import asyncio
import base64
import html
import time
import shutil
import uuid
import tempfile
from typing import Optional, Dict, Any, List
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    MenuButtonWebApp,
    InlineQuery,
    InlineQueryResultVideo,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InlineQueryResultPhoto,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    InputMediaPhoto,
    InputMediaVideo,
    ChosenInlineResult,
    CallbackQuery,
    FSInputFile
)
from downloader import (
    detect_platform,
    extract_url,
    download_video,
    parse_tiktok_tikwm,
    FFMPEG_PATH,
    DOWNLOAD_DIR,
    extract_direct_video_info,
    parse_instagram_embed,
    is_valid_mp4_file
)
import warp_proxy

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
        "traceback": traceback.format_exc()
    })
    if len(recent_errors) > 20:
        recent_errors.pop(0)


# ==========================================
# ⚙️ НАСТРОЙКИ (0 БЮДЖЕТА)
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rds0645010.github.io/duo-game-date/").strip()
ORGANIZER_CHAT_ID = os.getenv("ORGANIZER_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", 8080))
DB_PATH = "users.db"

# 👑 ПРИОРИТЕТ И 3 НЕЗАВИСИМЫЕ СИСТЕМЫ СКАЧИВАНИЯ
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS = set([x.strip() for x in ADMIN_ID_RAW.split(",") if x.strip()])

# 1. TikTok: лёгкий (не использует RAM/FFmpeg), до 5 параллельных потоков
TIKTOK_SEMAPHORE = asyncio.Semaphore(5)
tiktok_waiting_count = 0
tiktok_lock = asyncio.Lock()

# 2. Instagram: средний (~25-30 МБ RAM на процесс), до 2 параллельных потоков
INSTAGRAM_SEMAPHORE = asyncio.Semaphore(2)
instagram_waiting_count = 0
instagram_lock = asyncio.Lock()

# 3. YouTube: тяжёлый с FFmpeg-склейкой (~150 МБ), строго 1 для тарифа 512 МБ
try:
    MAX_YOUTUBE_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1") or "1"))
except ValueError:
    MAX_YOUTUBE_DOWNLOADS = 1
YOUTUBE_SEMAPHORE = asyncio.Semaphore(MAX_YOUTUBE_DOWNLOADS)
youtube_waiting_count = 0
youtube_lock = asyncio.Lock()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            photo_url TEXT,
            gender TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            sender_name TEXT,
            recipient_username TEXT,
            place TEXT,
            day TEXT,
            time TEXT,
            vibe TEXT,
            duration TEXT,
            note TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS video_cache (
            url TEXT PRIMARY KEY,
            file_id TEXT,
            media_type TEXT DEFAULT 'video',
            title TEXT,
            platform TEXT,
            author TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE video_cache ADD COLUMN media_type TEXT DEFAULT 'video'")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dl_tokens (
            token TEXT PRIMARY KEY,
            url TEXT,
            file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE dl_tokens ADD COLUMN file_path TEXT")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            canonical_key TEXT,
            file_id TEXT,
            media_type TEXT DEFAULT 'video',
            title TEXT,
            platform TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_downloads_user ON user_downloads(user_id, created_at DESC)")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            inline_caption INTEGER DEFAULT 0,
            save_inline_to_pm INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE user_settings ADD COLUMN save_inline_to_pm INTEGER DEFAULT 0")
    except Exception:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slideshow_sessions (
            sid TEXT PRIMARY KEY,
            url TEXT,
            platform TEXT,
            title TEXT,
            author TEXT,
            total INTEGER,
            file_ids_json TEXT,
            photo_urls_json TEXT,
            music TEXT,
            music_info_json TEXT,
            chat_id INTEGER,
            created_at REAL,
            digg_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0
        )
    """)
    try:
        cursor.execute("ALTER TABLE slideshow_sessions ADD COLUMN digg_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE slideshow_sessions ADD COLUMN comment_count INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()


def save_slideshow_session_db(sid: str, data: dict):
    """Персистентно сохраняет сессию слайдшоу в SQLite (переживает перезапуски Render)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO slideshow_sessions 
            (sid, url, platform, title, author, total, file_ids_json, photo_urls_json, music, music_info_json, chat_id, created_at, digg_count, comment_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sid,
            data.get("original_url", ""),
            data.get("platform", ""),
            data.get("title", ""),
            data.get("author", ""),
            data.get("total", 0),
            json.dumps(data.get("file_ids", [])),
            json.dumps(data.get("photo_urls", [])),
            data.get("music", ""),
            json.dumps(data.get("music_info") or {}),
            data.get("chat_id", 0),
            data.get("created_at", time.time()),
            data.get("digg_count", 0),
            data.get("comment_count", 0)
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"Failed to save slideshow session {sid}: {e}")


def get_slideshow_session_db(sid: str) -> Optional[dict]:
    """Загружает сессию слайдшоу: сначала из RAM, а при перезапуске — из SQLite."""
    data = _slideshow_cache.get(sid)
    if data:
        return data
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT url, platform, title, author, total, file_ids_json, photo_urls_json, music, music_info_json, chat_id, created_at, digg_count, comment_count
            FROM slideshow_sessions WHERE sid = ?
        """, (sid,))
        row = cursor.fetchone()
        conn.close()
        if row:
            u, plat, title, author, total, fids_j, urls_j, mus, mus_inf_j, chat_id, cr_at, digg, comm = row
            fids = json.loads(fids_j) if fids_j else []
            urls = json.loads(urls_j) if urls_j else []
            mus_inf = json.loads(mus_inf_j) if mus_inf_j else {}
            restored = {
                "photos": [],
                "file_ids": fids,
                "photo_urls": urls,
                "total": total,
                "title": title,
                "author": author,
                "platform": plat,
                "original_url": u,
                "music": mus,
                "music_info": mus_inf,
                "digg_count": digg or 0,
                "comment_count": comm or 0,
                "chat_id": chat_id,
                "created_at": cr_at
            }
            _slideshow_cache[sid] = restored
            return restored
    except Exception as e:
        # Fallback на случай старой таблицы
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT url, platform, title, author, total, file_ids_json, photo_urls_json, music, music_info_json, chat_id, created_at FROM slideshow_sessions WHERE sid = ?", (sid,))
            row = cursor.fetchone()
            conn.close()
            if row:
                u, plat, title, author, total, fids_j, urls_j, mus, mus_inf_j, chat_id, cr_at = row
                restored = {
                    "photos": [],
                    "file_ids": json.loads(fids_j) if fids_j else [],
                    "photo_urls": json.loads(urls_j) if urls_j else [],
                    "total": total,
                    "title": title,
                    "author": author,
                    "platform": plat,
                    "original_url": u,
                    "music": mus,
                    "music_info": json.loads(mus_inf_j) if mus_inf_j else {},
                    "digg_count": 0,
                    "comment_count": 0,
                    "chat_id": chat_id,
                    "created_at": cr_at
                }
                _slideshow_cache[sid] = restored
                return restored
        except Exception:
            pass
        logging.warning(f"Failed to load slideshow session {sid}: {e}")
    return None


def record_user_download(user_id: int, url: str, file_id: str, media_type: str, title: str, platform: str):
    """Сохраняет загрузку в личную историю пользователя (для мгновенной отправки через @ как в SaveBox)"""
    if not user_id or not file_id:
        return
    canon_k = get_canonical_media_key(url)
    clean_u = url.split("?")[0].strip().rstrip("/")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_downloads WHERE user_id = ? AND (canonical_key = ? OR url = ?)", (user_id, canon_k, clean_u))
        cursor.execute("""
            INSERT INTO user_downloads (user_id, url, canonical_key, file_id, media_type, title, platform)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, clean_u, canon_k, file_id, media_type, title, platform))
        conn.commit()
    except Exception as e:
        logging.warning(f"Could not record user download: {e}")
    finally:
        conn.close()


def get_user_recent_downloads(user_id: int, limit: int = 15):
    """Возвращает недавние загрузки пользователя для инлайн-режима"""
    if not user_id:
        return []
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    items = []
    try:
        cursor.execute("""
            SELECT file_id, media_type, title, platform, url 
            FROM user_downloads 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        for r in rows:
            items.append({
                "file_id": r[0],
                "media_type": r[1] or "video",
                "title": r[2] or "Медиа",
                "platform": r[3] or "video",
                "url": r[4]
            })
    except Exception as e:
        logging.warning(f"Could not get user recent downloads: {e}")
    finally:
        conn.close()
    return items


def save_dl_token(url: str, file_path: str = None) -> str:
    """Сохраняет URL и путь к файлу для прямого стриминга /stream/{token}.mp4"""
    if not url:
        return ""
    import uuid
    token = str(uuid.uuid4()).replace("-", "")[:12]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO dl_tokens (token, url, file_path) VALUES (?, ?, ?)", (token, url, file_path))
    conn.commit()
    conn.close()
    return token


def update_dl_token_file(token: str, file_path: str):
    if not token or not file_path:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE dl_tokens SET file_path = ? WHERE token = ?", (file_path, token))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.debug(f"update_dl_token_file error: {e}")


def get_dl_token_url(token: str):
    if not token:
        return None
    url, _ = get_dl_token_info(token)
    return url


def get_dl_token_info(token: str):
    if not token:
        return None, None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT url, file_path FROM dl_tokens WHERE token = ?", (token.strip(),))
        row = cursor.fetchone()
        if row:
            return row[0], row[1]
    except Exception:
        cursor.execute("SELECT url FROM dl_tokens WHERE token = ?", (token.strip(),))
        row = cursor.fetchone()
        if row:
            return row[0], None
    finally:
        conn.close()
    return None, None


def get_user_inline_caption(user_id: int) -> bool:
    if not user_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT inline_caption FROM user_settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row is not None:
        return bool(row[0])
    return False  # По умолчанию выключена: чистое видео без текста!


def set_user_inline_caption(user_id: int, enabled: bool):
    if not user_id:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_settings (user_id, inline_caption)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            inline_caption = excluded.inline_caption,
            updated_at = CURRENT_TIMESTAMP
    """, (user_id, 1 if enabled else 0))
    conn.commit()
    conn.close()


def get_user_save_to_pm(user_id: int) -> bool:
    if not user_id:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT save_inline_to_pm FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row is not None and row[0] is not None:
            return bool(row[0])
    except Exception:
        pass
    return False  # По умолчанию выключено: личка остаётся абсолютно чистой!


def set_user_save_to_pm(user_id: int, enabled: bool):
    if not user_id:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (user_id, save_inline_to_pm)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                save_inline_to_pm = excluded.save_inline_to_pm,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, 1 if enabled else 0))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_canonical_media_key(url: str) -> str:
    """Нормализует любые варианты ссылок одного и того же поста к общему ключу"""
    if not url:
        return ""
    import re
    u = url.strip()
    # 1. Instagram: ID шорткода (поддерживает /p/, /reel/, /reels/, /tv/, /share/)
    m_ig = re.search(r'(?:instagram\.com|instagr\.am)/(?:reel|p|reels|tv|share/(?:reel|p))/([A-Za-z0-9_-]+)', u)
    if m_ig:
        return f"ig_{m_ig.group(1)}"
    # 2. YouTube: 11-значный ID ролика (shorts, watch, youtu.be)
    m_yt = re.search(r'(?:v=|\/shorts\/|\/embed\/|youtu\.be\/|\/v\/)([A-Za-z0-9_-]{11})', u)
    if m_yt:
        return f"yt_{m_yt.group(1)}"
    # 3. TikTok: числовой ID видео
    m_tt = re.search(r'/video/(\d+)', u)
    if m_tt:
        return f"tt_{m_tt.group(1)}"
    return u.split("?")[0].strip().rstrip("/")


def get_cached_media(url: str):
    if not url:
        return None
    clean_u = url.split("?")[0].strip().rstrip("/")
    canon_k = get_canonical_media_key(url)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT file_id, title, platform, author, media_type FROM video_cache WHERE url = ? OR url = ? LIMIT 1",
            (canon_k, clean_u)
        )
        row = cursor.fetchone()
    except Exception:
        cursor.execute(
            "SELECT file_id, title, platform, author FROM video_cache WHERE url = ? OR url = ? LIMIT 1",
            (canon_k, clean_u)
        )
        row = cursor.fetchone()
        if row:
            row = (row[0], row[1], row[2], row[3], "video")
    conn.close()
    if row:
        return {
            "file_id": row[0],
            "title": row[1],
            "platform": row[2],
            "author": row[3],
            "media_type": row[4] if len(row) > 4 and row[4] else "video"
        }
    return None


def get_cached_video(url: str):
    return get_cached_media(url)


def save_cached_media(url: str, file_id: str, media_type: str, title: str, platform: str, author: str = ""):
    if not url or not file_id:
        return
    clean_u = url.split("?")[0].strip().rstrip("/")
    canon_k = get_canonical_media_key(url)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for u_key in set([clean_u, canon_k]):
        if not u_key:
            continue
        cursor.execute("""
            INSERT INTO video_cache (url, file_id, media_type, title, platform, author)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                file_id = excluded.file_id,
                media_type = excluded.media_type,
                title = excluded.title,
                platform = excluded.platform,
                author = excluded.author,
                created_at = CURRENT_TIMESTAMP
        """, (u_key, file_id, media_type, title, platform, author))
    conn.commit()
    conn.close()


def is_instagram_reel_url(url: str) -> bool:
    """Reels — это всегда видео. Фото-кэш для такого URL считается битым."""
    return bool(re.search(r'(?:instagram\.com|instagr\.am)/(?:reel|reels|tv|share/reel)/', url or "", re.IGNORECASE))


def delete_cached_media(url: str):
    """Удаляет запись видео-кэша для URL (включая канонический ключ ig_<code>)."""
    if not url:
        return
    clean_u = url.split("?")[0].strip().rstrip("/")
    canon_k = get_canonical_media_key(url)
    keys = [k for k in {clean_u, canon_k} if k]
    if not keys:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.executemany("DELETE FROM video_cache WHERE url = ?", [(k,) for k in keys])
        conn.commit()
        conn.close()
        logging.info(f"Removed cached media for {url}")
    except Exception as e:
        logging.warning(f"Could not delete cached media for {url}: {e}")


def drop_poisoned_instagram_photo_cache(url: str, cached: dict = None):
    """
    Если для reel в кэше лежит photo (обложка), выкидываем её и качаем заново.
    Возвращает True, если кэш был сброшен.
    """
    if not cached or not cached.get("file_id"):
        return False
    if cached.get("media_type") == "photo" and is_instagram_reel_url(url):
        logging.info(f"Dropping poisoned Instagram photo cache for reel: {url}")
        delete_cached_media(url)
        return True
    return False


def save_cached_video(url: str, file_id: str, title: str, platform: str, author: str = ""):
    save_cached_media(url, file_id, "video", title, platform, author)


def get_fallback_upload_chat():
    if ADMIN_IDS:
        for aid in ADMIN_IDS:
            try:
                return int(aid)
            except Exception:
                pass
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users ORDER BY created_at DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return int(row[0])
    except Exception:
        pass
    return None


OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0") or "0")
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID", "").strip()


async def upload_media_to_telegram(file_path: str, is_photo: bool = False, target_user_id: int = None, title: str = "", platform: str = ""):
    """
    Загружает медиафайл в Telegram для генерации постоянного CDN file_id.
    1. Если указан target_user_id — пробует отправить в ЛС пользователя (сохраняя как личный архив).
    2. Если пользователь не запускал бота (Forbidden) — отправляет владельцу бота (OWNER_USER_ID) и сразу удаляет.
    3. Если и это недоступно — отправляет в STORAGE_CHANNEL_ID или fallback chat.
    """
    if not file_path or not os.path.exists(file_path):
        return None

    caption_text = None
    if target_user_id:
        t_clean = html.escape((title or "Видео")[:150])
        p_clean = html.escape((platform or "Медиа").capitalize())
        caption_text = (
            f"🎬 <b>{t_clean}</b>\n\n"
            f"📱 Источник: {p_clean}\n"
            f"✨ <i>Сохранено при отправке через @duo_play_date_bot</i>"
        )

    should_save_in_pm = get_user_save_to_pm(target_user_id) if target_user_id else False

    # 1. Пробуем отправить в ЛС пользователя
    if target_user_id:
        try:
            if is_photo:
                sent = await bot.send_photo(
                    chat_id=target_user_id,
                    photo=FSInputFile(file_path),
                    caption=caption_text if should_save_in_pm else None,
                    parse_mode="HTML" if (caption_text and should_save_in_pm) else None,
                    disable_notification=True
                )
                if sent and sent.photo:
                    f_id = sent.photo[-1].file_id
                    if not should_save_in_pm:
                        asyncio.create_task(_delayed_delete_message(target_user_id, sent.message_id, 2))
                    return f_id
            else:
                sent = await bot.send_video(
                    chat_id=target_user_id,
                    video=FSInputFile(file_path),
                    caption=caption_text if should_save_in_pm else None,
                    parse_mode="HTML" if (caption_text and should_save_in_pm) else None,
                    supports_streaming=True,
                    disable_notification=True
                )
                if sent and sent.video:
                    f_id = sent.video.file_id
                    if not should_save_in_pm:
                        asyncio.create_task(_delayed_delete_message(target_user_id, sent.message_id, 2))
                    return f_id
        except Exception as u_err:
            logging.info(f"Target user {target_user_id} upload failed ({u_err}), trying silent fallback...")

    # 2. Бесшумный fallback: OWNER_USER_ID или fallback chat с мгновенным удалением
    fallbacks = []
    if OWNER_USER_ID and OWNER_USER_ID != target_user_id:
        fallbacks.append(OWNER_USER_ID)
    if STORAGE_CHANNEL_ID:
        try: fallbacks.append(int(STORAGE_CHANNEL_ID))
        except Exception: pass
    fb_chat = get_fallback_upload_chat()
    if fb_chat and fb_chat not in fallbacks and fb_chat != target_user_id:
        fallbacks.append(fb_chat)

    for chat_id in fallbacks:
        try:
            if is_photo:
                sent = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(file_path), disable_notification=True)
                if sent and sent.photo:
                    f_id = sent.photo[-1].file_id
                    if chat_id > 0:
                        asyncio.create_task(_delayed_delete_message(chat_id, sent.message_id, 60))
                    return f_id
            else:
                sent = await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), supports_streaming=True, disable_notification=True)
                if sent and sent.video:
                    f_id = sent.video.file_id
                    if chat_id > 0:
                        asyncio.create_task(_delayed_delete_message(chat_id, sent.message_id, 60))
                    return f_id
        except Exception as fb_err:
            logging.debug(f"Fallback upload chat {chat_id} error: {fb_err}")

    return None


async def _delayed_delete_message(chat_id: int, message_id: int, delay_seconds: int = 60):
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def save_user(user_id: int, username: str, first_name: str, photo_url: str = None, gender: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    clean_username = (username or "").lower().replace("@", "").strip()
    cursor.execute("""
        INSERT INTO users (user_id, username, first_name, photo_url, gender)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            photo_url = COALESCE(excluded.photo_url, users.photo_url),
            gender = COALESCE(excluded.gender, users.gender),
            updated_at = CURRENT_TIMESTAMP
    """, (user_id, clean_username, first_name, photo_url, gender))
    conn.commit()
    conn.close()


def find_user_by_username(username: str):
    if not username:
        return None
    s = str(username).strip()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Если передан числовой user_id
    if s.isdigit():
        cursor.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (int(s),))
        row = cursor.fetchone()
        if row:
            conn.close()
            return row
    clean = s.lower().replace("@", "").strip()
    cursor.execute("SELECT user_id, first_name FROM users WHERE username = ?", (clean,))
    row = cursor.fetchone()
    conn.close()
    return row  # (user_id, first_name)


def create_invite_record(sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    clean_recipient = (recipient_username or "").lower().replace("@", "").strip()
    cursor.execute("""
        INSERT INTO invites (sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (sender_id, sender_name, clean_recipient, place, day, time, vibe, duration, note))
    invite_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return invite_id


def get_invite_record(invite_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note, status
        FROM invites WHERE id = ?
    """, (invite_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "sender_id": row[1],
            "sender_name": row[2],
            "recipient_username": row[3],
            "place": row[4],
            "day": row[5],
            "time": row[6],
            "vibe": row[7],
            "duration": row[8],
            "note": row[9],
            "status": row[10]
        }
    return None


init_db()

# Бот и Диспетчер
bot_session = AiohttpSession(timeout=180.0)
bot = Bot(token=BOT_TOKEN, session=bot_session) if (BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE") else None
dp = Dispatcher()


# Окружение: Render или локальный запуск
IS_ON_RENDER = bool(
    os.getenv("RENDER")
    or os.getenv("RENDER_SERVICE_ID")
    or os.getenv("RENDER_INSTANCE_ID")
)

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
if not RENDER_EXTERNAL_URL:
    RENDER_EXTERNAL_URL = "https://duo-game-date-bot.onrender.com"
if not RENDER_EXTERNAL_URL.startswith("http://") and not RENDER_EXTERNAL_URL.startswith("https://"):
    RENDER_EXTERNAL_URL = f"https://{RENDER_EXTERNAL_URL}"
RENDER_EXTERNAL_URL = RENDER_EXTERNAL_URL.rstrip("/")

# Режим работы бота: Webhook или Polling (по умолчанию везде Polling для надёжной доставки ЛС и инлайна)
# Принудительное управление через USE_WEBHOOK=true/false
_use_wh_env = os.getenv("USE_WEBHOOK", "").strip().lower()
if _use_wh_env in ("1", "true", "yes"):
    USE_WEBHOOK = True
else:
    USE_WEBHOOK = False  # По умолчанию всегда Polling (даже на Render) для стабильного ответа в ЛС

WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or None


def get_app_url():
    base = WEBAPP_URL
    if RENDER_EXTERNAL_URL:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}srv={RENDER_EXTERNAL_URL}"
    return base


async def safe_edit_status(msg: types.Message, text: str):
    """Безопасное обновление статуса без вылета по ошибкам Telegram."""
    if not msg:
        return
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        logging.debug(f"Status edit ignored: {e}")


def create_progress_reporter(status_msg: types.Message, platform_name: str, throttle_seconds: float = 1.3):
    """
    Создает троттлинговый прогресс-репортер для редактирования статусного сообщения Telegram.
    Троттлинг 1.3-1.5 сек предотвращает ошибки Telegram 429 Flood Control.
    """
    last_update = 0.0
    last_rendered_pct = -1.0

    async def update_progress(downloaded: int, total: int, speed: float = 0.0, eta: float = 0.0):
        nonlocal last_update, last_rendered_pct
        if not status_msg:
            return

        now = time.time()

        # Финализация / склейка потоков
        if downloaded == -1:
            if now - last_update >= 1.0:
                last_update = now
                await safe_edit_status(
                    status_msg,
                    f"⚙️ <b>Обрабатываю {platform_name}...</b>\n"
                    f"<code>[████████████] 100%</code>\n"
                    f"Финализирую файл и готовлю к отправке..."
                )
            return

        if total <= 0:
            if now - last_update >= throttle_seconds:
                last_update = now
                cur_mb = downloaded / (1024 * 1024)
                spd_str = f" • {speed / (1024 * 1024):.1f} МБ/с" if speed > 0 else ""
                await safe_edit_status(
                    status_msg,
                    f"⚡ <b>Скачиваю {platform_name}...</b>\n"
                    f"📊 Загружено: {cur_mb:.1f} МБ{spd_str}"
                )
            return

        pct = min(100.0, max(0.0, (downloaded / total) * 100.0))

        # Не спамим правками, если прошло мало времени и процент почти не изменился
        if (now - last_update < throttle_seconds) and abs(pct - last_rendered_pct) < 15:
            return

        last_update = now
        last_rendered_pct = pct

        bar_len = 12
        filled = int(round(bar_len * pct / 100.0))
        bar = "█" * filled + "░" * (bar_len - filled)

        cur_mb = downloaded / (1024 * 1024)
        tot_mb = total / (1024 * 1024)

        spd_str = f" • {speed / (1024 * 1024):.1f} МБ/с" if speed > 0 else ""
        eta_str = f"\n⏱ Осталось: ~{max(1, int(round(eta)))} сек" if eta > 0 else ""

        text = (
            f"⚡ <b>Скачиваю {platform_name}...</b>\n"
            f"<code>[{bar}] {int(pct)}%</code>\n"
            f"📊 {cur_mb:.1f} из {tot_mb:.1f} МБ{spd_str}{eta_str}"
        )
        await safe_edit_status(status_msg, text)

    return update_progress


def format_friendly_error(e: Exception) -> str:
    """Преобразует любые технические ошибки в понятные объяснения для пользователя."""
    err_text = str(e).lower()
    if "is_photo_post" in err_text:
        return (
            "📸 <b>Не удалось извлечь фото из этой публикации Instagram.</b>\n\n"
            "Возможно, публикация удалена, является приватной или автор ограничил доступ для гостей."
        )
    elif "instagram_login_required" in err_text or "empty media response" in err_text:
        return (
            "🔒 <b>Instagram ограничил доступ к этой публикации.</b>\n\n"
            "Instagram закрыл анонимный просмотр (публикация 18+, из закрытого аккаунта или автор ограничил доступ).\n\n"
            "💡 <i>Публикации из TikTok и YouTube Shorts скачиваются всегда и без ограничений!</i>"
        )
    elif "instagram_video_unavailable" in err_text:
        return (
            "🎬 <b>Instagram не отдал видеофайл.</b>\n\n"
            "Сервер Instagram не предоставил доступ к потоку этого ролика (возможно, он удален или закрыт для прямого скачивания).\n\n"
            "💡 <i>Попробуй отправить ролик из TikTok или YouTube Shorts — они работают моментально!</i>"
        )
    elif "private" in err_text or "login" in err_text or "401" in err_text or "403" in err_text:
        return (
            "🔒 <b>Это видео из закрытого (приватного) профиля.</b>\n\n"
            "Telegram не может получить доступ к закрытым профилям без пароля. "
            "Скачивать можно только видео из открытых публичных аккаунтов."
        )
    elif "confirm your age" in err_text or "age-restricted" in err_text or "instagram_age_restricted" in err_text or "available to everyone" in err_text or "certain audiences" in err_text:
        return (
            "🔞 <b>Это видео имеет возрастное ограничение (18+) в Instagram.</b>\n\n"
            "Instagram закрыл доступ к этому ролику для гостей и требует подтверждения возраста.\n\n"
            "💡 <i>Администратор может указать cookies аккаунта (INSTAGRAM_COOKIES) в настройках хостинга, чтобы бот мог загружать контент 18+.</i>"
        )
    elif "confirm you’re not a bot" in err_text or "confirm you're not a bot" in err_text or "youtube_bot_detection" in err_text:
        return (
            "🤖 <b>YouTube временно ограничил прямое скачивание с облачного сервера.</b>\n\n"
            "Попробуй отправить ролик ещё раз через пару секунд или отправить другое видео/Shorts.\n\n"
            "<i>(Instagram Reels и ролики из TikTok работают мгновенно!)</i>"
        )
    elif "file_too_large" in err_text or "too large" in err_text:
        return (
            "⚠️ <b>Файл превышает 50 МБ (лимит Telegram).</b>\n\n"
            "Telegram Bot API запрещает ботам отправлять файлы больше 50 МБ. Попробуй более короткий ролик."
        )
    elif "live event" in err_text or "live stream" in err_text:
        return (
            "🔴 <b>Это ссылка на прямой эфир (стрим).</b>\n\n"
            "Бот может скачивать только завершенные опубликованные видео, а не идущую трансляцию."
        )
    elif "unavailable" in err_text or "not found" in err_text or "404" in err_text:
        return (
            "🗑 <b>Видео не найдено или было удалено автором.</b>\n\n"
            "Проверь, открывается ли ссылка в обычном браузере."
        )
    elif "timed out" in err_text or "timeout" in err_text:
        return (
            "⏱ <b>Сервер источника не ответил вовремя.</b>\n\n"
            "Возможно, временный сбой на стороне платформы. Попробуй еще раз через минуту."
        )
    else:
        return f"❌ <b>Не удалось скачать видео.</b>\n\n⚠️ Детали: <code>{html.escape(str(e)[:150])}</code>"


# 🖼 КЭШ СЛАЙДШОУ (КАРУСЕЛЕЙ) И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
_slideshow_cache = {}

async def _expire_slideshow_session(sid: str, delay_seconds: int = 3600):
    """Удаляет временные файлы со слайдами через 1 час, когда сессия просмотра завершена."""
    await asyncio.sleep(delay_seconds)
    data = _slideshow_cache.pop(sid, None)
    if data and data.get("photos"):
        for p in data["photos"]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

async def _preupload_slideshow_file_ids(sid: str):
    """Фоновая предварительная загрузка всех слайдов в Telegram для мгновенного перелистывания без лагов."""
    data = _slideshow_cache.get(sid)
    if not data or not data.get("photos"):
        return
    for idx, p_path in enumerate(data["photos"]):
        if idx >= len(data.get("file_ids", [])):
            break
        if data["file_ids"][idx] is None and p_path and os.path.exists(p_path):
            try:
                is_vid = p_path.lower().endswith((".mp4", ".mov", ".m4v"))
                fid = await upload_media_to_telegram(p_path, is_photo=not is_vid)
                if fid and sid in _slideshow_cache:
                    _slideshow_cache[sid]["file_ids"][idx] = fid
            except Exception as up_err:
                logging.debug(f"Preupload slide #{idx} failed: {up_err}")

def format_count(n) -> str:
    """Форматирует число в компактный вид: 368 -> 368, 51200 -> 51.2K, 1500000 -> 1.5M"""
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
        if k_val >= 100:
            return f"{int(k_val)}K"
        s = f"{k_val:.1f}K"
        return s.replace(".0K", "K")
    m_val = val / 1_000_000.0
    if m_val >= 100:
        return f"{int(m_val)}M"
    s = f"{m_val:.1f}M"
    return s.replace(".0M", "M")


def make_slide_caption(data: dict, current_idx: int, total: int) -> str:
    """
    Генерирует аккуратную, компактную подпись к слайду фото-карусели:
    без кучи пустых строк и без дублирования номера слайда (номер уже показан на кнопке).
    """
    plat_raw = (data.get("platform") or "TikTok").capitalize()
    title_raw = (data.get("title") or "").strip()

    # Схлопываем множественные переносы строк в один пробел
    title_clean = re.sub(r'[\r\n]+', ' ', title_raw).strip()
    if len(title_clean) > 200:
        title_clean = title_clean[:200].rstrip() + "..."

    title_esc = html.escape(title_clean)
    author_raw = (data.get("author") or "").strip()[:50]
    author_esc = html.escape(author_raw)

    lines = []
    if title_esc and title_esc.lower() not in (
        "instagram альбом", "tiktok слайдшоу", "медиа", "tiktok фото",
        "instagram публикация", "публикация", "фото", "видео"
    ):
        lines.append(f"📸 <b>{title_esc}</b>")

    meta = f"📱 <b>Источник:</b> {plat_raw}"
    if author_esc and author_esc.lower() not in ("tiktok", "instagram", "media", "видео"):
        meta += f" | {author_esc}"
    lines.append(meta)

    lines.append("✨ <i>Скачано через @duo_play_date_bot</i>")
    # Используем \n вместо \n\n, чтобы не было гигантских пустых пробелов
    return "\n".join(lines)


def build_slideshow_keyboard(sid: str, current_idx: int, total: int, data: dict, is_inline: bool = False) -> InlineKeyboardMarkup:
    """
    Создает клавиатуру управления слайдшоу в точности как у @SavXBot:
    Ряд 1: [ 🔘 / ⬅️ ]  [ 1/X ]  [ ➡️ / 🔘 ]
    Ряд 2: [ ❤️ 51.2K ↗️ ]  [ 💬 368 ↗️ ]  [ 💌 ⤴️ ]  [ 🎵 ↗️ ]
    Ряд 3 (только в ЛС): [ 📥 Скачать все слайды альбомом ] (и [ 🎧 Скачать MP3 ] если есть трек)
    """
    # Ряд 1: Интерактивная навигация (активные стрелки / неактивные кружочки)
    btn_prev = InlineKeyboardButton(
        text="⬅️" if current_idx > 0 else "🔘",
        callback_data=f"slide:{sid}:{current_idx - 1}" if current_idx > 0 else "slide_noop"
    )
    btn_counter = InlineKeyboardButton(
        text=f"{current_idx + 1}/{total}",
        callback_data=f"slide_info:{current_idx + 1}:{total}"
    )
    btn_next = InlineKeyboardButton(
        text="➡️" if current_idx < total - 1 else "🔘",
        callback_data=f"slide:{sid}:{current_idx + 1}" if current_idx < total - 1 else "slide_noop"
    )
    row1 = [btn_prev, btn_counter, btn_next]

    # Ряд 2: Точно как у @SavXBot:
    # 4 кнопки в одну строку: Лайки, Комментарии, Шеринг, Звук
    orig_url = data.get("original_url") or ""
    post_url = orig_url if orig_url.startswith("http") else "https://t.me"

    digg_cnt = data.get("digg_count") or data.get("likes") or 0
    comm_cnt = data.get("comment_count") or data.get("comments") or 0

    digg_str = format_count(digg_cnt)
    comm_str = format_count(comm_cnt)

    btn_likes = InlineKeyboardButton(
        text=f"❤️ {digg_str}" if digg_str else "❤️",
        url=post_url
    )
    btn_comments = InlineKeyboardButton(
        text=f"💬 {comm_str}" if comm_str else "💬",
        url=post_url
    )
    btn_share = InlineKeyboardButton(
        text="💌",
        switch_inline_query=orig_url
    )

    row2 = [btn_likes, btn_comments, btn_share]

    m_info = data.get("music_info") or {}
    m_id = m_info.get("id") if isinstance(m_info, dict) else None
    music_u = data.get("music") or (m_info.get("play") if isinstance(m_info, dict) else None)

    if m_id:
        row2.append(InlineKeyboardButton(text="🎵", url=f"https://www.tiktok.com/music/-{m_id}"))
    elif music_u and str(music_u).startswith("http"):
        row2.append(InlineKeyboardButton(text="🎵", url=str(music_u)))
    elif music_u:
        row2.append(InlineKeyboardButton(text="🎵", callback_data=f"music:{sid}"))

    rows = [row1, row2]

    # Ряд 3: Только для личного чата
    if not is_inline:
        row3 = [
            InlineKeyboardButton(
                text="📥 Скачать все слайды альбомом",
                callback_data=f"album_all:{sid}"
            )
        ]
        if music_u:
            row3.append(
                InlineKeyboardButton(
                    text="🎧 Скачать MP3",
                    callback_data=f"music:{sid}"
                )
            )
        rows.append(row3)

    return InlineKeyboardMarkup(inline_keyboard=rows)



async def process_video_download(message: types.Message, url: str):
    clean_url = extract_url(url) or url
    platform = detect_platform(clean_url)
    logging.info(f"🚀 [СКАЧИВАНИЕ] Старт: {clean_url} (платформа: {platform}, от user: {message.from_user.id})")
    if not platform:
        await message.answer("⚠️ Не удалось определить платформу. Поддерживаются: TikTok, YouTube Shorts, Reels.")
        return

    # 1. Проверяем кэш (если видео/фото уже скачивалось ранее — отдаём МГНОВЕННО)
    user_id = message.from_user.id
    share_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↗️ Отправить в чат через @",
                    switch_inline_query=clean_url
                )
            ]
        ]
    )

    cached = get_cached_media(clean_url)
    if drop_poisoned_instagram_photo_cache(clean_url, cached):
        cached = None
    if cached and ("/photo/" in clean_url or "/p/" in clean_url):
        logging.info(f"Bypassing cache for potential album: {clean_url}")
        cached = None
    if cached and cached.get("file_id"):
        title_esc = html.escape(cached.get("title") or "Медиа")
        plat_esc = html.escape(cached.get('platform', 'Медиа').capitalize())
        f_id = cached["file_id"]
        m_type = cached.get("media_type", "video")
        record_user_download(user_id, clean_url, f_id, m_type, cached.get("title", ""), cached.get("platform", ""))

        if m_type == "photo":
            caption = (
                f"📸 <b>{title_esc}</b>\n\n"
                f"📱 Источник: {plat_esc}\n"
                f"✨ <i>Скачано через @duo_play_date_bot (мгновенно из кэша ⚡)</i>"
            )
            try:
                await message.answer_photo(
                    photo=f_id,
                    caption=caption,
                    reply_markup=share_kb,
                    parse_mode="HTML"
                )
                return
            except Exception as e:
                logging.warning(f"Failed to send cached photo file_id {f_id}: {e}")
        else:
            caption = (
                f"🎬 <b>{title_esc}</b>\n\n"
                f"📱 Источник: {plat_esc}\n"
                f"✨ <i>Скачано через @duo_play_date_bot без водяных знаков и рекламы (мгновенно из кэша ⚡)</i>"
            )
            try:
                await message.answer_video(
                    video=f_id,
                    caption=caption,
                    reply_markup=share_kb,
                    parse_mode="HTML"
                )
                return
            except Exception as e:
                logging.warning(f"Failed to send cached video file_id {f_id}: {e}")
            # Если Telegram удалил file_id или ошибка — скачиваем заново

    is_vip = str(user_id) in ADMIN_IDS

    target_platform = detect_platform(clean_url) or "youtube"
    if target_platform == "tiktok":
        sem = TIKTOK_SEMAPHORE
        lock = tiktok_lock
        p_name = "TikTok"
    elif target_platform == "instagram":
        sem = INSTAGRAM_SEMAPHORE
        lock = instagram_lock
        p_name = "Instagram Reels"
    else:
        sem = YOUTUBE_SEMAPHORE
        lock = youtube_lock
        p_name = "YouTube Shorts"

    status_msg = None
    queue_acquired = False
    res = None

    try:
        # Проверяем занятость независимого слота платформы
        if sem.locked() and not is_vip:
            async with lock:
                if target_platform == "tiktok":
                    global tiktok_waiting_count
                    tiktok_waiting_count += 1
                    pos = tiktok_waiting_count
                elif target_platform == "instagram":
                    global instagram_waiting_count
                    instagram_waiting_count += 1
                    pos = instagram_waiting_count
                else:
                    global youtube_waiting_count
                    youtube_waiting_count += 1
                    pos = youtube_waiting_count
            status_msg = await message.answer(
                f"⏳ <b>В очереди на скачивание {p_name}...</b>\n"
                f"Твоя позиция: {pos}. Ожидаем завершения предыдущих загрузок 🚀",
                parse_mode="HTML"
            )
        else:
            status_msg = await message.answer(
                f"⚡ <b>Обрабатываю {p_name}...</b>\n"
                "Скачиваем в максимальном качестве без водяных знаков...",
                parse_mode="HTML"
            )

        # VIP пользователи имеют безусловный приоритет
        if is_vip:
            queue_acquired = True
            await safe_edit_status(status_msg, f"👑 <b>VIP приоритет активен!</b>\nСкачиваю {p_name} вне очереди...")
        else:
            await sem.acquire()
            queue_acquired = True
            async with lock:
                if target_platform == "tiktok" and tiktok_waiting_count > 0:
                    tiktok_waiting_count -= 1
                elif target_platform == "instagram" and instagram_waiting_count > 0:
                    instagram_waiting_count -= 1
                elif target_platform == "youtube" and youtube_waiting_count > 0:
                    youtube_waiting_count -= 1
            await safe_edit_status(status_msg, f"⚡ <b>Скачиваю {p_name}...</b>\nОчищаю от водяных знаков и готовлю к отправке...")

        # Выполняем скачивание с живым прогресс-баром
        progress_cb = create_progress_reporter(status_msg, p_name)
        res = await download_video(clean_url, user_id, progress_callback=progress_cb)
        if not res or not isinstance(res, dict):
            await safe_edit_status(status_msg, "❌ <b>Не удалось скачать видео.</b>\nВозможно, видео удалено или автор ограничил доступ.")
            return

        # 🖼 СНАЧАЛА проверяем: если это фото-слайдшоу (альбом картинок)
        if res.get("is_album") and (res.get("photos") or res.get("photo_urls")):
            photos = res.get("photos") or []
            photo_urls = res.get("photo_urls") or []
            total_slides = max(len(photos), len(photo_urls))
            sid = uuid.uuid4().hex[:10]
            plat_name = res.get("platform", "tiktok")
            default_ttl = f"{plat_name.capitalize()} Слайдшоу"
            _slideshow_cache[sid] = {
                "photos": photos,
                "photo_urls": photo_urls,
                "file_ids": [None] * total_slides,
                "total": total_slides,
                "title": res.get("title") or default_ttl,
                "author": res.get("author") or "",
                "platform": plat_name,
                "original_url": clean_url,
                "music": res.get("music"),
                "music_info": res.get("music_info"),
                "digg_count": res.get("digg_count", 0),
                "comment_count": res.get("comment_count", 0),
                "chat_id": message.chat.id,
                "created_at": time.time()
            }
            save_slideshow_session_db(sid, _slideshow_cache[sid])

            first_media = FSInputFile(photos[0]) if (photos and os.path.exists(photos[0])) else photo_urls[0]
            caption = make_slide_caption(_slideshow_cache[sid], 0, total_slides)
            kb = build_slideshow_keyboard(sid, 0, total_slides, _slideshow_cache[sid])
            is_vid = str(first_media).lower().endswith((".mp4", ".mov", ".m4v"))
            if is_vid:
                sent = await message.answer_video(
                    video=first_media,
                    caption=caption,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
                if sent and sent.video:
                    _slideshow_cache[sid]["file_ids"][0] = sent.video.file_id
                    save_slideshow_session_db(sid, _slideshow_cache[sid])
            else:
                sent = await message.answer_photo(
                    photo=first_media,
                    caption=caption,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
                if sent and sent.photo:
                    _slideshow_cache[sid]["file_ids"][0] = sent.photo[-1].file_id
                    save_slideshow_session_db(sid, _slideshow_cache[sid])

            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        file_path = res.get("file_path")

        if not file_path or not os.path.exists(file_path):
            await safe_edit_status(status_msg, "❌ Не удалось загрузить видео файл. Возможно, видео удалено или автор ограничил доступ.")
            return

        file_size = os.path.getsize(file_path)

        platform_names = {
            "tiktok": "TikTok",
            "youtube": "YouTube Shorts",
            "instagram": "Instagram Reels"
        }
        plat_str = platform_names.get(res.get("platform"), "Видео")

        title_raw = (res.get("title") or "Видео")[:200]
        author_raw = (res.get("author") or "")[:50]
        title_esc = html.escape(title_raw)
        author_esc = html.escape(author_raw)

        # 🎧 Если это длинный ролик YouTube, сконвертированный в аудио-подкаст
        if res.get("is_audio"):
            try:
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VOICE)
            except Exception:
                pass
            file_size_mb = round(file_size / (1024 * 1024), 1)
            await safe_edit_status(
                status_msg,
                f"🎧 <b>Отправляю аудиодорожку в Telegram ({file_size_mb} МБ)...</b>"
            )
            audio_caption = (
                f"🎧 <b>{title_esc}</b>\n\n"
                f"📱 <b>Источник:</b> {plat_str} (Аудио / Подкаст)\n"
                f"ℹ️ <i>(Ролик больше 50 МБ — видео-лимит Telegram, поэтому отправлен как чистая аудиозапись для удобного прослушивания)</i>\n"
                f"✨ <i>Скачано через @duo_play_date_bot</i>"
            )
            audio_input = FSInputFile(file_path)
            sent = await message.answer_audio(
                audio=audio_input,
                caption=audio_caption,
                title=title_raw[:80],
                performer=author_raw[:50],
                duration=int(res.get("duration", 0)),
                reply_markup=share_kb,
                parse_mode="HTML"
            )
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # Проверка размера (Telegram Bot API лимит 50 МБ)
        if file_size > 50 * 1024 * 1024:
            await safe_edit_status(
                status_msg,
                f"⚠️ <b>Файл слишком большой ({round(file_size / (1024*1024), 1)} МБ)</b>\n\n"
                f"Telegram Bot API не позволяет отправлять ботам видео больше 50 МБ.\n"
                f"Попробуй более короткое видео или шортс."
            )
            return

        file_size_mb = round(file_size / (1024 * 1024), 1)
        await safe_edit_status(status_msg, f"📤 <b>Отправляю чистое видео в Telegram ({file_size_mb} МБ)...</b>")
        try:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass

        # 📸 Если в посте оказалась одиночная фотография
        if res.get("is_photo"):
            photo_caption = (
                f"📸 <b>{title_esc}</b>\n\n"
                f"📱 <b>Источник:</b> {plat_str}\n"
                f"ℹ️ <i>(В публикации было фото, а не видео — сохранено в исходном качестве)</i>\n"
                f"✨ <i>Скачано через @duo_play_date_bot</i>"
            )
            photo_input = FSInputFile(file_path)
            sent = await message.answer_photo(
                photo=photo_input,
                caption=photo_caption,
                reply_markup=share_kb,
                parse_mode="HTML"
            )
            if sent and sent.photo:
                f_id = sent.photo[-1].file_id
                save_cached_media(
                    clean_url,
                    f_id,
                    "photo",
                    res.get("title", ""),
                    res.get("platform", ""),
                    res.get("author", "")
                )
                record_user_download(
                    user_id,
                    clean_url,
                    f_id,
                    "photo",
                    res.get("title", ""),
                    res.get("platform", "")
                )
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        caption = f"🎬 <b>{title_esc}</b>\n\n📱 <b>Источник:</b> {plat_str}"
        if author_esc:
            caption += f" | {author_esc}"
        caption += "\n✨ <i>Скачано через @duo_play_date_bot без водяных знаков и рекламы</i>"

        if res.get("low_quality_notice"):
            caption += "\n\n<i>(ℹ️ Качество сжато до 360p, так как исходное видео превышало лимиты)</i>"

        sent = None
        # Для TikTok: пробуем мгновенную отправку напрямую по CDN URL (1-2 сек вместо 20 сек)
        if target_platform == "tiktok" and res.get("direct_url") and not res.get("is_photo") and not res.get("is_album"):
            try:
                sent = await message.answer_video(
                    video=res["direct_url"],
                    caption=caption,
                    reply_markup=share_kb,
                    parse_mode="HTML"
                )
            except Exception as direct_send_err:
                logging.info(f"TikTok direct URL send bypassed ({direct_send_err}), sending local file...")
                sent = None

        if not sent:
            video_input = FSInputFile(file_path)
            try:
                sent = await message.answer_video(
                    video=video_input,
                    caption=caption,
                    reply_markup=share_kb,
                    parse_mode="HTML"
                )
            except Exception as html_send_err:
                logging.warning(f"HTML caption send error: {html_send_err}, sending plain caption...")
                sent = await message.answer_video(
                    video=video_input,
                    caption=f"🎬 {title_raw[:100]}\n✨ Скачано через @duo_play_date_bot",
                    reply_markup=share_kb
                )

        # Сохраняем file_id в кэш для мгновенных ответов в будущем
        if sent and sent.video and sent.video.file_id:
            v_id = sent.video.file_id
            save_cached_media(
                clean_url,
                v_id,
                "video",
                res.get("title", ""),
                res.get("platform", ""),
                res.get("author", "")
            )
            record_user_download(
                user_id,
                clean_url,
                v_id,
                "video",
                res.get("title", ""),
                res.get("platform", "")
            )

        # Удаляем сервисное сообщение со статусом
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        record_error("process_video_download", e, clean_url)
        logging.error(f"Download processing failed for {clean_url}: {e}", exc_info=True)
        msg = format_friendly_error(e)

        if status_msg:
            try:
                await status_msg.edit_text(msg, parse_mode="HTML")
            except Exception:
                await message.answer(msg, parse_mode="HTML")
        else:
            await message.answer(msg, parse_mode="HTML")


    finally:
        # Освобождаем независимый семафор соответствующей платформы
        if queue_acquired and not is_vip:
            sem.release()
        # Удаляем временные файлы с диска ТОЛЬКО для одиночных роликов (альбомы живут в _slideshow_cache 1 час)
        if res and isinstance(res, dict) and not res.get("is_album"):
            fp = res.get("file_path")
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception as del_err:
                        logging.warning(f"Could not remove temp file {fp}: {del_err}")


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject = None):
    user = message.from_user
    logging.info(f"🚀 [КОМАНДА /start] от {user.id} (@{user.username or user.first_name}) args={command.args if command else None}")
    save_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name
    )

    app_link = get_app_url()

    # 🔗 Обработка перехода по ссылке на быстрое скачивание (?start=dl_...)
    if command and command.args and command.args.startswith("dl_"):
        try:
            token_or_b64 = command.args.replace("dl_", "").strip()
            target_url = get_dl_token_url(token_or_b64)
            if not target_url:
                try:
                    raw_b64 = token_or_b64
                    missing_padding = len(raw_b64) % 4
                    if missing_padding:
                        raw_b64 += '=' * (4 - missing_padding)
                    target_url = base64.b64decode(raw_b64).decode('utf-8')
                except Exception:
                    target_url = None

            if target_url and detect_platform(target_url):
                await process_video_download(message, target_url)
                return
        except Exception as e:
            logging.error(f"Error handling dl_ start: {e}")

    # 🔗 Обработка перехода по умной вирусной ссылке (?start=inv_123)
    if command and command.args and command.args.startswith("inv_"):
        try:
            inv_id_str = command.args.replace("inv_", "").strip()
            if inv_id_str.isdigit():
                inv = get_invite_record(int(inv_id_str))
                if inv:
                    sender_name = inv["sender_name"] or "Твой тиммейт"
                    invite_msg = (
                        f"👾 **ТЕБЯ ЗОВУТ В ИГРУ В DUO!** 🎮\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 **От кого:** {sender_name}\n"
                        f"🕹 **Игра:** {inv['place']}\n"
                        f"📅 **Время:** {inv['day']} в {inv['time']}\n"
                        f"✨ **Вайб:** {inv['vibe']}\n"
                        f"⏳ **Сколько играем:** {inv['duration']}\n"
                    )
                    if inv["note"] and inv["note"] != "Без записки":
                        invite_msg += f"💌 **Записка:** _{inv['note']}_\n"
                    invite_msg += (
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"Ты в деле? Выбери ответ ниже 👇"
                    )

                    reply_kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Я в деле! Погнали!",
                                    callback_data=f"accept_{inv['sender_id'] or 0}_{inv['id']}"
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="⏰ Давай, но позже",
                                    callback_data=f"later_{inv['sender_id'] or 0}_{inv['id']}"
                                ),
                                InlineKeyboardButton(
                                    text="💔 Не сегодня",
                                    callback_data=f"decline_{inv['sender_id'] or 0}_{inv['id']}"
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="🎮 Открыть DUO ✨",
                                    web_app=WebAppInfo(url=app_link)
                                )
                            ]
                        ]
                    )
                    await message.answer(invite_msg, reply_markup=reply_kb, parse_mode="Markdown")
                    return
        except Exception as e:
            logging.error(f"Error handling deep link invite: {e}")

    welcome_text = (
        f"👾 **DUO — ИГРЫ И ЗАГРУЗЧИК ВИДЕО БЕЗ РЕКЛАМЫ** 🎮\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Привет, {user.first_name}! 👋\n\n"
        f"✨ **Совместные игры в DUO:**\n"
        f"• Зови тиммейтов в любимые игры за 2 клика\n"
        f"• Назначай тайминги и собирай дуэты\n\n"
        f"📥 **Загрузка видео без рекламы и водяных знаков:**\n"
        f"• Отправь сюда любую ссылку на **TikTok, YouTube Shorts или Reels**\n"
        f"• В чатах с друзьями пиши: `@duo_play_date_bot <ссылка>`, чтобы сразу переслать видео напарнику!\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Открой лобби игр ниже или скинь ссылку на видео прямо в чат 👇"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎮 Открыть лобби DUO ✨",
                    web_app=WebAppInfo(url=app_link)
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ Настройки подписи в чатах",
                    callback_data="open_settings"
                )
            ]
        ]
    )

    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="🎮 DUO",
                web_app=WebAppInfo(url=app_link)
            )
        )
    except Exception as e:
        logging.warning(f"Menu button warning: {e}")

    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")


def get_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    is_caption = get_user_inline_caption(user_id)
    is_pm_save = get_user_save_to_pm(user_id)

    cap_btn = "🟢 Подпись: Включена" if is_caption else "⚪ Подпись: Выключена"
    toggle_cap = "0" if is_caption else "1"

    pm_btn = "🟢 Архив в ЛС: Включен" if is_pm_save else "⚪ Архив в ЛС: Выключен (чистая личка)"
    toggle_pm = "0" if is_pm_save else "1"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{cap_btn} (сменить)",
                    callback_data=f"toggle_caption_{toggle_cap}"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{pm_btn} (сменить)",
                    callback_data=f"toggle_pm_{toggle_pm}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎮 Открыть лобби DUO ✨",
                    web_app=WebAppInfo(url=get_app_url())
                )
            ]
        ]
    )


def get_settings_text(user_id: int) -> str:
    is_caption = get_user_inline_caption(user_id)
    is_pm_save = get_user_save_to_pm(user_id)

    mode_descr = (
        "🟢 <b>Включена</b> — под видео будет автор, название и ссылка"
        if is_caption else
        "⚪ <b>Выключена (по умолчанию)</b> — видео отправляется абсолютно чистым, без текста"
    )

    pm_descr = (
        "🟢 <b>Включен</b> — копии отправленных через @ видео сохраняются в вашей переписке с ботом"
        if is_pm_save else
        "⚪ <b>Выключен (по умолчанию)</b> — личка бота остаётся чистой, сообщения мгновенно удаляются после отправки другу"
    )

    return (
        "⚙️ <b>НАСТРОЙКИ ЗАГРУЗЧИКА И DUO</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "💬 <b>Подпись к видео (при отправке через @):</b>\n"
        f"{mode_descr}\n\n"
        "📥 <b>Сохранение видео в ЛС бота:</b>\n"
        f"{pm_descr}\n\n"
        "<i>Нажмите на кнопку ниже, чтобы переключить режим 👇</i>"
    )


@dp.message(Command("settings"))
@dp.message(F.text == "/settings")
@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    user_id = message.from_user.id
    await message.answer(
        get_settings_text(user_id),
        reply_markup=get_settings_keyboard(user_id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "open_settings")
async def handle_open_settings(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    try:
        await callback.message.edit_text(
            get_settings_text(user_id),
            reply_markup=get_settings_keyboard(user_id),
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            get_settings_text(user_id),
            reply_markup=get_settings_keyboard(user_id),
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("toggle_caption_"))
async def callback_toggle_caption(callback: types.CallbackQuery):
    new_val = callback.data.replace("toggle_caption_", "") == "1"
    set_user_inline_caption(callback.from_user.id, new_val)

    tip = "🟢 Подпись к видео включена!" if new_val else "⚪ Чистый режим активирован (без текста)!"
    await callback.answer(tip)
    try:
        await callback.message.edit_text(
            get_settings_text(callback.from_user.id),
            reply_markup=get_settings_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("toggle_pm_"))
async def callback_toggle_pm(callback: types.CallbackQuery):
    new_val = callback.data.replace("toggle_pm_", "") == "1"
    set_user_save_to_pm(callback.from_user.id, new_val)

    tip = "🟢 Архив в ЛС включен!" if new_val else "⚪ Личка будет чистой (видео не сохраняются в ЛС)!"
    await callback.answer(tip)
    try:
        await callback.message.edit_text(
            get_settings_text(callback.from_user.id),
            reply_markup=get_settings_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
    except Exception:
        pass



@dp.callback_query(F.data.startswith("accept_"))
async def handle_accept(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None
    
    await callback.answer("Приглашение принято! 🎉")
    await callback.message.edit_text(
        callback.message.text + "\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✅ **Ты в деле! Запускай игру! 🚀**\n"
        "Твой напарник уже получил уведомление.",
        parse_mode="Markdown"
    )

    if sender_id and bot:
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=(
                    f"🎉 **ЕСТЬ КОНТАКТ! ТИММЕЙТ В ДЕЛЕ!** 🚀\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 **{callback.from_user.full_name}** принял(а) твой инвайт в DUO!\n"
                    f"Готовьтесь к совместной кате 🎮✨"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


@dp.callback_query(F.data.startswith("later_"))
async def handle_later(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None

    await callback.answer("Ответ отправлен ⏰")
    await callback.message.edit_text(
        callback.message.text + "\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "⏰ **Ответ:** Давай, но чуть попозже!",
        parse_mode="Markdown"
    )

    if sender_id and bot:
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=(
                    f"⏰ **ОТВЕТ ОТ НАПАРНИКА**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 **{callback.from_user.full_name}** не против сыграть, но предлагает чуть позже!"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


@dp.callback_query(F.data.startswith("decline_"))
async def handle_decline(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None

    await callback.answer("Отклонено 💔")
    await callback.message.edit_text(
        callback.message.text + "\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💔 **Ответ:** Сегодня не получится.",
        parse_mode="Markdown"
    )

    if sender_id and bot:
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=(
                    f"💔 **ОТВЕТ ОТ НАПАРНИКА**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 **{callback.from_user.full_name}** сегодня не сможет пойти в игру."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


_stream_files = {}   # token -> local_file_path
_stream_locks = {}   # token -> asyncio.Lock
# Даже если несколько пользователей одновременно вставят ссылки в inline,
# качаем не больше одного видео: на 512 МБ это критично.
_precache_semaphore = asyncio.Semaphore(1)


async def _expire_stream_file(token: str, path: str, delay: int = 900):
    """Через 15 минут удаляет неиспользованный pre-cache, чтобы не забить диск."""
    try:
        await asyncio.sleep(delay)
        if _stream_files.get(token) != path:
            return
        _stream_files.pop(token, None)
        if os.path.exists(path):
            os.remove(path)
            logging.info(f"Pre-cache cleanup: removed {path}")
    except Exception:
        pass


async def pre_cache_video_to_disk(clean_url: str, token: str):
    """
    Фоновая предварительная загрузка видео на диск в temp_downloads,
    чтобы оно было готово к моменту запроса серверами Telegram.
    """
    if token in _stream_files and os.path.exists(_stream_files[token]):
        return
    # Чистим ссылки на уже удалённые файлы, чтобы лимит не «залипал».
    for stale_token in [k for k, v in _stream_files.items() if not os.path.exists(v)]:
        _stream_files.pop(stale_token, None)
    # Не запускаем десятки фоновых скачиваний, если пользователи просто
    # перебирают ссылки в inline-режиме: на 512 МБ это лишняя нагрузка.
    if len(_stream_files) >= 5:
        logging.info("Pre-cache skipped: already 5 files pending")
        return
    lock = _stream_locks.setdefault(token, asyncio.Lock())
    async with lock:
        if token in _stream_files and os.path.exists(_stream_files[token]):
            return
        async with _precache_semaphore:
            if token in _stream_files and os.path.exists(_stream_files[token]):
                return
            if len(_stream_files) >= 5:
                logging.info("Pre-cache skipped inside semaphore: 5 files pending")
                return
            try:
                res = await download_video(clean_url, 0)
                if res and res.get("file_path") and os.path.exists(res["file_path"]):
                    _stream_files[token] = res["file_path"]
                    asyncio.create_task(_expire_stream_file(token, res["file_path"]))
            except Exception as e:
                logging.warning(f"Pre-cache error for {clean_url}: {e}")


async def handle_stream_video(request: web.Request) -> web.StreamResponse:
    """
    HTTP endpoint для отдачи видео напрямую серверам Telegram (RFC 7233 Range requests).
    Когда пользователь отправляет InlineQueryResultVideo в чат, Telegram скачивает MP4 отсюда.
    """
    media_key = request.match_info.get("media_key", "")
    token = media_key.replace(".mp4", "").replace(".jpg", "").strip()

    target_url, db_file_path = get_dl_token_info(token)
    if not target_url and not db_file_path:
        return web.Response(text="Token not found or expired", status=404)

    file_path = _stream_files.get(token) or db_file_path
    if not file_path or not os.path.exists(file_path):
        for fname in os.listdir(DOWNLOAD_DIR):
            if token in fname and (fname.endswith(".mp4") or fname.endswith(".jpg")):
                cand = os.path.join(DOWNLOAD_DIR, fname)
                if os.path.exists(cand) and os.path.getsize(cand) > 500:
                    file_path = cand
                    _stream_files[token] = file_path
                    break

    if not file_path or not os.path.exists(file_path):
        lock = _stream_locks.setdefault(token, asyncio.Lock())
        async with lock:
            file_path = _stream_files.get(token)
            if not file_path or not os.path.exists(file_path):
                try:
                    res = await download_video(target_url, 0)
                    if res and res.get("file_path") and os.path.exists(res["file_path"]):
                        file_path = res["file_path"]
                        _stream_files[token] = file_path
                        update_dl_token_file(token, file_path)
                except Exception as e:
                    logging.error(f"Stream handler download error: {e}")
                    return web.Response(text=f"Download error: {e}", status=502)

    if file_path and os.path.exists(file_path):
        content_type = "video/mp4" if file_path.endswith(".mp4") else "image/jpeg"
        return web.FileResponse(
            file_path,
            headers={
                "Content-Type": content_type,
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400"
            }
        )
    return web.Response(text="File not found", status=404)


# ==========================================
# 📥 ИНЛАЙН РЕЖИМ (@duo_play_date_bot <ссылка>)
# ==========================================
@dp.inline_query()
async def inline_query_handler(inline_query: InlineQuery):
    query = inline_query.query.strip()
    user_id = inline_query.from_user.id
    caption_enabled = get_user_inline_caption(user_id)

    # 1. Если запрос ПУСТОЙ — подсказка
    if not query:
        results = [
            InlineQueryResultArticle(
                id="hint_empty",
                title="📥 Вставь ссылку на видео или фото",
                description="TikTok, YouTube Shorts или Instagram Reels/Фото",
                thumbnail_url="https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/youtube.png",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        "💡 <b>Как отправить видео или фото другу через @duo_play_date_bot:</b>\n\n"
                        "Вставь ссылку на TikTok, Shorts или Instagram после имени бота:\n"
                        "<code>@duo_play_date_bot https://...</code>\n\n"
                        "И нажми на появившуюся кнопку — чистое медиа улетит прямо в чат!"
                    ),
                    parse_mode="HTML"
                )
            )
        ]
        await inline_query.answer(results=results, cache_time=5, is_personal=True)
        return

    clean_url = extract_url(query)
    platform = detect_platform(clean_url) if clean_url else None

    if not platform or not clean_url:
        results = [
            InlineQueryResultArticle(
                id="hint_unsupported",
                title="⚠️ Ссылка не распознана",
                description="Поддерживаются: TikTok, YouTube Shorts, Instagram Reels",
                thumbnail_url="https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/youtube.png",
                input_message_content=InputTextMessageContent(
                    message_text="⚠️ Отправьте прямую ссылку на TikTok, YouTube Shorts или Instagram Reels!"
                )
            )
        ]
        await inline_query.answer(results=results, cache_time=10, is_personal=True)
        return

    # Названия и официальные иконки платформ (стиль SavXBot)
    if platform == "instagram":
        plat_noun = "Reel"
        plat_source = "Instagram"
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/instagram.png"
    elif platform == "tiktok":
        plat_noun = "видео"
        plat_source = "TikTok"
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tiktok.png"
    elif platform == "youtube":
        plat_noun = "Shorts"
        plat_source = "YouTube"
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/youtube.png"
    else:
        plat_noun = "видео"
        plat_source = "Видео"
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/youtube.png"

    inline_title = f"💌 Поделиться {plat_noun} из {plat_source}"

    def _make_caption(title_text: str, is_photo: bool = False) -> str:
        if not caption_enabled:
            return None
        t = html.escape((title_text or plat_noun)[:200])
        icon = "📸" if is_photo else "🎬"
        return f"{icon} <b>{t}</b>\n\n📱 {plat_source} | ✨ <i>через @duo_play_date_bot</i>"

    # ─── 2. КЭШ: если видео уже есть — МГНОВЕННО ───
    cached = get_cached_media(clean_url)
    if drop_poisoned_instagram_photo_cache(clean_url, cached):
        cached = None
    if cached and ("/photo/" in clean_url or "/p/" in clean_url):
        logging.info(f"Bypassing cache for potential album: {clean_url}")
        cached = None
    if cached and cached.get("file_id"):
        m_type = cached.get("media_type", "video")
        cap = _make_caption(cached.get("title"), is_photo=(m_type == "photo"))
        if m_type == "photo":
            single_result = InlineQueryResultCachedPhoto(
                id=f"c_p_{abs(hash(cached['file_id']))}",
                photo_file_id=cached["file_id"],
                title=inline_title,
                caption=cap,
                parse_mode="HTML" if cap else None
            )
        else:
            single_result = InlineQueryResultCachedVideo(
                id=f"c_v_{abs(hash(cached['file_id']))}",
                video_file_id=cached["file_id"],
                title=inline_title,
                caption=cap,
                parse_mode="HTML" if cap else None
            )
        await inline_query.answer(results=[single_result], cache_time=300, is_personal=True)
        return

    # ─── 3. БЫСТРЫЙ ПРЯМОЙ ПОТОК (Только для TikTok) ───
    # Для TikTok CDN отдает видео серверам Telegram без блокировок.
    # Для Instagram и YouTube прямые CDN-ссылки НЕЛЬЗЯ отдавать в InlineQueryResultVideo,
    # так как CDN Instagram и YouTube блокируют серверы Telegram (403), вызывая восклицательный знак (!).
    direct_video_url = None
    direct_title = None

    if platform == "tiktok":
        try:
            direct_info = await asyncio.wait_for(parse_tiktok_tikwm(clean_url), timeout=4.0)
            if direct_info and direct_info.get("video_url"):
                direct_video_url = direct_info["video_url"]
                direct_title = direct_info.get("title")
        except Exception as e:
            logging.debug(f"TikTok direct parse failed: {e}")

    if direct_video_url:
        cap = _make_caption(direct_title)
        single_result = InlineQueryResultVideo(
            id=f"dir_tt_{abs(hash(clean_url))}",
            video_url=direct_video_url,
            mime_type="video/mp4",
            thumbnail_url=plat_icon,
            title=inline_title,
            caption=cap,
            parse_mode="HTML" if cap else None
        )
        await inline_query.answer(results=[single_result], cache_time=30, is_personal=True)
        return

    # ─── 4. ДВУХФАЗНАЯ ПОДМЕНА (Стиль SavXBot: 100% надежно для Instagram и YouTube) ───
    # Если ролик ещё не в кэше — отдаём аккуратную карточку с кнопкой-индикатором.
    # При клике Telegram отправляет ChosenInlineResult (или юзер жмет кнопку), бот скачивает видео
    # и подменяет сообщение на чистое нативное видео через edit_message_media без битых ссылок!
    token = save_dl_token(clean_url)
    import uuid
    fresh_id = f"art_v4_{token}_{uuid.uuid4().hex[:6]}"

    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Скачиваем видео...", callback_data=f"get_vid:{token}")]
        ]
    )

    article_result = InlineQueryResultArticle(
        id=fresh_id,
        title=inline_title,
        description="Нажми, чтобы чистое видео улетело прямо в чат ✨",
        thumbnail_url=plat_icon,
        input_message_content=InputTextMessageContent(
            message_text=f"⏳ <b>Загружаю {plat_noun} из {plat_source}...</b>\n<i>Скоро появится в этом сообщении ✨</i>",
            parse_mode="HTML"
        ),
        reply_markup=loading_kb
    )

    # Запускаем фоновую докачку на сервере, чтобы к моменту клика файл уже скачивался
    asyncio.create_task(pre_cache_video_to_disk(clean_url, token))

    try:
        await inline_query.answer(results=[article_result], cache_time=0, is_personal=True)
    except Exception as ans_err:
        logging.error(f"Error answering inline query: {ans_err}")


# ==========================================
# 🔄 ОБРАБОТКА ВЫБОРА ИНЛАЙН-РЕЗУЛЬТАТА (SavXBot паттерн)
# ==========================================
_active_inline_jobs = set()

async def execute_inline_delivery(inline_message_id: str, token: str, user_id: int):
    """Единая функция выполнения скачивания и подмены сообщения в инлайне."""
    job_key = f"{inline_message_id}_{token}"
    if job_key in _active_inline_jobs:
        return
    _active_inline_jobs.add(job_key)

    try:
        url = get_dl_token_url(token)
        if not url:
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text="⚠️ <b>Ссылка устарела.</b> Пожалуйста, вызовите бота заново через инлайн.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        cached = get_cached_media(url)
        file_id = cached.get("file_id") if cached else None
        is_photo = (cached.get("media_type") == "photo") if cached else False
        if file_id and is_photo and is_instagram_reel_url(url):
            logging.info(f"Dropping poisoned Instagram photo cache in inline: {url}")
            delete_cached_media(url)
            file_id = None
            is_photo = False

        if not file_id:
            try:
                # 1. Проверяем, успела ли фоновая предзагрузка скачать файл на диск
                pre_path = _stream_files.get(token)
                if pre_path and os.path.exists(pre_path) and is_valid_mp4_file(pre_path):
                    res = {
                        "file_path": pre_path,
                        "title": "Видео",
                        "platform": detect_platform(url) or ""
                    }
                else:
                    res = await download_video(url, user_id)

                # 🖼 Обработка каруселей/слайдшоу в инлайн-режиме
                if res and isinstance(res, dict) and res.get("is_album") and (res.get("photos") or res.get("photo_urls")):
                    photos = res.get("photos") or []
                    photo_urls = res.get("photo_urls") or []
                    total_slides = max(len(photos), len(photo_urls))
                    sid = uuid.uuid4().hex[:10]
                    plat_name = res.get("platform", "tiktok")
                    default_ttl = f"{plat_name.capitalize()} Слайдшоу"
                    _slideshow_cache[sid] = {
                        "photos": photos,
                        "photo_urls": photo_urls,
                        "file_ids": [None] * total_slides,
                        "total": total_slides,
                        "title": res.get("title") or default_ttl,
                        "author": res.get("author") or "",
                        "platform": plat_name,
                        "original_url": url,
                        "music": res.get("music"),
                        "music_info": res.get("music_info"),
                        "digg_count": res.get("digg_count", 0),
                        "comment_count": res.get("comment_count", 0),
                        "chat_id": 0,
                        "created_at": time.time()
                    }
                    save_slideshow_session_db(sid, _slideshow_cache[sid])

                    first_file = photos[0] if (photos and os.path.exists(photos[0])) else None
                    first_u = photo_urls[0] if photo_urls else None
                    first_fid = await upload_media_to_telegram(first_file, is_photo=True, target_user_id=user_id) if first_file else None
                    if first_fid:
                        _slideshow_cache[sid]["file_ids"][0] = first_fid
                        save_slideshow_session_db(sid, _slideshow_cache[sid])

                    media_target = first_fid or first_u or (FSInputFile(first_file) if first_file else None)
                    caption = make_slide_caption(_slideshow_cache[sid], 0, total_slides)
                    kb = build_slideshow_keyboard(sid, 0, total_slides, _slideshow_cache[sid], is_inline=True)
                    is_vid = str(media_target).lower().endswith((".mp4", ".mov", ".m4v"))
                    media_cls = InputMediaVideo if is_vid else InputMediaPhoto
                    if media_target:
                        await bot.edit_message_media(
                            inline_message_id=inline_message_id,
                            media=media_cls(media=media_target, caption=caption, parse_mode="HTML"),
                            reply_markup=kb
                        )
                    _stream_files.pop(token, None)
                    return

                if res and isinstance(res, dict) and res.get("file_path"):
                    is_photo = bool(res.get("is_photo"))
                    file_id = await upload_media_to_telegram(
                        res["file_path"],
                        is_photo=is_photo,
                        target_user_id=user_id,
                        title=res.get("title", ""),
                        platform=res.get("platform", "")
                    )
                    if file_id:
                        save_cached_media(
                            url, file_id,
                            "photo" if is_photo else "video",
                            res.get("title", ""),
                            res.get("platform", ""),
                            res.get("author", "")
                        )
                    try:
                        if os.path.exists(res["file_path"]):
                            os.remove(res["file_path"])
                    except Exception:
                        pass
                    # Больше не держим ссылку на уже удалённый pre-cache файл.
                    _stream_files.pop(token, None)
                else:
                    raise Exception("download_failed")
            except Exception as e:
                logging.error(f"Inline download error for {url}: {e}")
                err_text = format_friendly_error(e)
                retry_kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"retry_dl:{token}")]]
                )
                try:
                    await bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=f"{err_text}\n\n<i>Нажмите кнопку ниже для повторной попытки 👇</i>",
                        parse_mode="HTML",
                        reply_markup=retry_kb
                    )
                except Exception:
                    pass
                return

        if file_id:
            caption_enabled = get_user_inline_caption(user_id)
            caption = None
            if caption_enabled:
                caption = "🎬 ✨ <i>Скачано через @duo_play_date_bot</i>"

            try:
                media_obj = (
                    InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML" if caption else None)
                    if is_photo
                    else InputMediaVideo(media=file_id, caption=caption, parse_mode="HTML" if caption else None, supports_streaming=True)
                )
                await bot.edit_message_media(
                    inline_message_id=inline_message_id,
                    media=media_obj,
                    reply_markup=None
                )
                logging.info(f"Successfully replaced inline message {inline_message_id} with media!")
            except Exception as edit_err:
                logging.error(f"Failed to edit_message_media: {edit_err}")
    finally:
        _active_inline_jobs.discard(job_key)


@dp.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult):
    """
    Срабатывает сразу после того, как пользователь нажал на кнопку в инлайне.
    Бот скачивает видео, загружает его в Telegram и подменяет текстовую плашку
    'Загружаю видео...' на готовое чистое видео!
    """
    if not chosen.inline_message_id:
        return

    rid = chosen.result_id
    if rid.startswith("art_v4_"):
        parts = rid.split("_")
        token = parts[2]
    else:
        token = rid.replace("ytv2_", "").replace("tok_", "").strip()
    await execute_inline_delivery(chosen.inline_message_id, token, chosen.from_user.id)


@dp.callback_query(F.data.startswith("get_vid:"))
async def handle_get_vid_callback(callback: CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer()
        return
    token = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Скачиваю видео в чат...", show_alert=False)
    await execute_inline_delivery(callback.inline_message_id, token, callback.from_user.id)


@dp.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery):
    await callback.answer("⏳ Видео обрабатывается и появится в этом сообщении через пару секунд!", show_alert=False)


@dp.callback_query(F.data.startswith("retry_dl:"))
async def handle_retry_callback(callback: CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer()
        return
    token = callback.data.split(":", 1)[1]
    await callback.answer("🔄 Перезапускаю загрузку...")
    try:
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text="⏳ <b>Повторная загрузка видео...</b>\n<i>Пожалуйста, подождите ✨</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⏳ Скачиваем видео...", callback_data=f"get_vid:{token}")]]
            )
        )
    except Exception:
        pass
    await execute_inline_delivery(callback.inline_message_id, token, callback.from_user.id)


# ==========================================
# 🖼 ОБРАБОТЧИКИ ИНТЕРАКТИВНОГО СЛАЙДШОУ (КАРУСЕЛИ)
# ==========================================

@dp.callback_query(F.data.startswith("slide:"))
async def handle_slide_switch_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    sid = parts[1]
    try:
        new_idx = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    logging.info(f"🖼 [СЛАЙДШОУ] Переключение: sid={sid}, на слайд={new_idx}")

    data = get_slideshow_session_db(sid)
    if not data:
        # Сессия не найдена в памяти и БД (например, старое сообщение до обновления)
        # Добавляем кнопку прямо в сообщение для перезагрузки!
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Загрузить слайды заново", callback_data=f"refresh_slide:{sid}")
        ]])
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
            elif callback.inline_message_id:
                await bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id, reply_markup=kb)
        except Exception:
            pass
        await callback.answer("⚠️ Сессия устарела. Нажмите '🔄 Загрузить слайды заново' 👇", show_alert=True)
        return

    total = data.get("total", 1)
    if new_idx < 0 or new_idx >= total:
        await callback.answer()
        return

    caption = make_slide_caption(data, new_idx, total)
    is_in = bool(callback.inline_message_id)
    kb = build_slideshow_keyboard(sid, new_idx, total, data, is_inline=is_in)

    file_ids = data.get("file_ids") or []
    photo_urls = data.get("photo_urls") or []
    photos = data.get("photos") or []

    cached_fid = file_ids[new_idx] if new_idx < len(file_ids) else None
    photo_u = photo_urls[new_idx] if new_idx < len(photo_urls) else None
    file_path = photos[new_idx] if new_idx < len(photos) else None

    # Приоритет источника:
    # 1. Telegram file_id (самый быстрый, проверенный CDN)
    # 2. Прямой CDN URL фото (Telegram загружает сам напрямую)
    # 3. Локальный файл на диске
    media_target = cached_fid or photo_u

    # Если переключение в инлайне
    if callback.inline_message_id:
        if not media_target and file_path and os.path.exists(file_path):
            cached_fid = await upload_media_to_telegram(file_path, is_photo=True)
            if cached_fid:
                if new_idx < len(file_ids):
                    data["file_ids"][new_idx] = cached_fid
                    save_slideshow_session_db(sid, data)
                media_target = cached_fid

        if media_target:
            is_vid = str(media_target).lower().endswith((".mp4", ".mov", ".m4v"))
            media_cls = InputMediaVideo if is_vid else InputMediaPhoto
            try:
                await bot.edit_message_media(
                    inline_message_id=callback.inline_message_id,
                    media=media_cls(media=media_target, caption=caption, parse_mode="HTML"),
                    reply_markup=kb
                )
            except Exception as in_err:
                logging.debug(f"Inline slide edit error: {in_err}")
        await callback.answer()
        return

    # Если переключение в личном чате или группе
    if not media_target and file_path and os.path.exists(file_path):
        media_target = FSInputFile(file_path)

    if not media_target:
        await callback.answer("❌ Слайд временно недоступен.", show_alert=True)
        return

    is_vid = str(file_path or media_target).lower().endswith((".mp4", ".mov", ".m4v"))
    media_cls = InputMediaVideo if is_vid else InputMediaPhoto
    media_obj = media_cls(media=media_target, caption=caption, parse_mode="HTML")

    try:
        if callback.message:
            res_msg = await callback.message.edit_media(media=media_obj, reply_markup=kb)
            if res_msg and res_msg.photo and new_idx < len(data["file_ids"]):
                data["file_ids"][new_idx] = res_msg.photo[-1].file_id
                save_slideshow_session_db(sid, data)
        await callback.answer()
    except Exception as e:
        logging.warning(f"Slide edit media error: {e}")
        # Если Telegram отклонил FSInputFile в edit_media — загружаем через send_photo и подменяем по file_id
        if file_path and os.path.exists(file_path) and callback.message:
            try:
                tmp_sent = await bot.send_photo(chat_id=callback.message.chat.id, photo=FSInputFile(file_path), disable_notification=True)
                if tmp_sent and tmp_sent.photo:
                    new_fid = tmp_sent.photo[-1].file_id
                    await tmp_sent.delete()
                    if new_idx < len(data["file_ids"]):
                        data["file_ids"][new_idx] = new_fid
                        save_slideshow_session_db(sid, data)
                    await callback.message.edit_media(
                        media=InputMediaPhoto(media=new_fid, caption=caption, parse_mode="HTML"),
                        reply_markup=kb
                    )
            except Exception as retry_err:
                logging.debug(f"Slide edit media retry error: {retry_err}")
        await callback.answer()


@dp.callback_query(F.data.startswith("refresh_slide:"))
async def handle_refresh_slide_callback(callback: CallbackQuery):
    sid = callback.data.split(":")[1] if ":" in callback.data else ""
    await callback.answer("🔄 Проверяю сессию...", show_alert=False)
    data = get_slideshow_session_db(sid)
    if data:
        total = data.get("total", 1)
        is_in = bool(callback.inline_message_id)
        kb = build_slideshow_keyboard(sid, 0, total, data, is_inline=is_in)
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
            elif callback.inline_message_id:
                await bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id, reply_markup=kb)
        except Exception:
            pass
    else:
        await callback.answer("ℹ️ Отправьте ссылку заново в чат, чтобы создать новую активную сессию.", show_alert=True)


@dp.callback_query(F.data.startswith("stat:"))
async def handle_slide_stat_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    sid = parts[1] if len(parts) > 1 else ""
    data = _slideshow_cache.get(sid)
    if not data:
        await callback.answer("📊 Статистика недоступна", show_alert=False)
        return
    digg = data.get("digg_count", 0)
    comm = data.get("comment_count", 0)
    shares = data.get("share_count", 0)
    plat = (data.get("platform") or "Публикации").capitalize()
    alert_text = (
        f"📊 Статистика {plat}:\n\n"
        f"❤️ Лайков: {digg:,}\n"
        f"💬 Комментариев: {comm:,}\n"
    )
    if shares > 0:
        alert_text += f"🔄 Репостов: {shares:,}\n"
    alert_text = alert_text.replace(",", " ")
    await callback.answer(alert_text, show_alert=True)


@dp.callback_query(F.data.startswith("music:"))
async def handle_slide_music_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    sid = parts[1] if len(parts) > 1 else ""
    data = _slideshow_cache.get(sid)
    if not data or not data.get("music"):
        await callback.answer("🎵 В этом посте нет отдельной аудиодорожки.", show_alert=True)
        return

    music_url = data["music"]
    music_info = data.get("music_info") or {}
    m_title = (music_info.get("title") or data.get("title") or "TikTok Audio")[:80]
    m_author = (music_info.get("author") or data.get("author") or "TikTok")[:50]
    m_dur = int(music_info.get("duration", 0))

    await callback.answer("🎵 Отправляю аудиодорожку...")
    try:
        import tiktok_service
        tmp_audio = os.path.join(tempfile.gettempdir(), f"tt_music_{sid}.mp3")
        if not os.path.exists(tmp_audio):
            await tiktok_service.download_file_stream(music_url, tmp_audio)
        audio_file = FSInputFile(tmp_audio)
        target_chat = callback.message.chat.id if callback.message else callback.from_user.id
        await bot.send_audio(
            chat_id=target_chat,
            audio=audio_file,
            title=m_title,
            performer=m_author,
            duration=m_dur,
            caption=f"🎵 <b>{html.escape(m_title)}</b>\n👤 {html.escape(m_author)}\n✨ <i>@duo_play_date_bot</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Failed to send tiktok audio: {e}")
        target_chat = callback.message.chat.id if callback.message else callback.from_user.id
        try:
            await bot.send_message(chat_id=target_chat, text=f"⚠️ Не удалось отправить аудиодорожку: {e}")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("album_all:"))
async def handle_slide_album_all_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    sid = parts[1] if len(parts) > 1 else ""
    data = _slideshow_cache.get(sid)
    if not data or not data.get("photos"):
        await callback.answer("⚠️ Сессия слайдшоу устарела.", show_alert=True)
        return

    photos = data["photos"]
    await callback.answer("📥 Отправляю все слайды альбомом...")
    plat_raw = (data.get("platform") or "Медиа").capitalize()
    default_title = f"{plat_raw} Альбом"
    title_esc = html.escape((data.get("title") or default_title)[:200])
    author_raw = (data.get("author") or "")[:50]
    author_esc = html.escape(author_raw)
    album_caption = (
        f"📸 <b>{title_esc}</b>\n\n"
        f"📱 <b>Источник:</b> {plat_raw}"
    )
    if author_esc and author_esc.lower() not in ("tiktok", "instagram", "media", "видео"):
        album_caption += f" | {author_esc}"
    album_caption += (
        f"\n🖼 <b>Всего слайдов:</b> {len(photos)}\n"
        f"✨ <i>Скачано через @duo_play_date_bot</i>"
    )

    for chunk_start in range(0, len(photos), 10):
        chunk = photos[chunk_start:chunk_start + 10]
        media_group = []
        for i, p_path in enumerate(chunk):
            if os.path.exists(p_path):
                p_file = FSInputFile(p_path)
                is_vid = p_path.lower().endswith((".mp4", ".mov", ".m4v"))
                media_cls = InputMediaVideo if is_vid else InputMediaPhoto
                if chunk_start == 0 and i == 0:
                    media_group.append(media_cls(media=p_file, caption=album_caption, parse_mode="HTML"))
                else:
                    media_group.append(media_cls(media=p_file))
        if media_group:
            try:
                target_chat = callback.message.chat.id if callback.message else callback.from_user.id
                await bot.send_media_group(chat_id=target_chat, media=media_group)
            except Exception as e:
                logging.warning(f"Failed to send media group chunk: {e}")


@dp.callback_query(F.data.startswith("slide_info:"))
async def handle_slide_info_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) == 3:
        await callback.answer(f"Слайд {parts[1]} из {parts[2]}", show_alert=False)
    else:
        await callback.answer()


@dp.callback_query(F.data == "slide_noop")
async def handle_slide_noop_callback(callback: CallbackQuery):
    await callback.answer()


# ==========================================
# 💬 ОБРАБОТЧИК СООБЩЕНИЙ В ЛС БОТА
# ==========================================
@dp.message(F.text | F.caption)
async def handle_text_messages(message: types.Message):
    # Проверяем наличие ссылок на видео как в тексте, так и в подписи (при пересылке/шеринге)
    raw_text = message.text or message.caption or ""
    u_name = message.from_user.username or message.from_user.first_name or "no_user"
    logging.info(f"📩 [ЛС от {message.from_user.id} (@{u_name})]: {raw_text[:120]}")
    url = extract_url(raw_text)
    if url and detect_platform(url):
        await process_video_download(message, url)
        return

    # Если обычное текстовое сообщение
    app_link = get_app_url()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎮 Открыть лобби DUO ✨",
                    web_app=WebAppInfo(url=app_link)
                )
            ]
        ]
    )
    await message.answer(
        "👾 **Я умею две вещи:**\n\n"
        "1. 📥 **Скачивать видео без водяных знаков и рекламы** — просто отправь мне ссылку на **TikTok, YouTube Shorts или Instagram Reels**!\n\n"
        "2. 🎮 **Организовывать совместные игры в DUO** — открой лобби по кнопке ниже и пригласи напарника в пару кликов!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ==========================================
# 🌐 ВСТРОЕННЫЙ HTTP API ДЛЯ САЙТА (MINI APP)
# ==========================================
async def handle_health(request):
    warp_status = warp_proxy.warp.status()
    return web.json_response({
        "status": "ok",
        "app": "DUO 24/7 Bot Server",
        "bot_configured": bool(bot),
        "mode": "webhook" if USE_WEBHOOK else "polling",
        "webhook_url": f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if USE_WEBHOOK else None,
        "warp_enabled": warp_status["enabled"],
        "warp_ready": warp_status["ready"],
    })


async def handle_api_warp(request):
    """Диагностика WARP: GET /api/warp (или ?check=1, чтобы поднять прокси сразу)."""
    if request.query.get("check", "").lower() in ("1", "true", "yes", "on"):
        try:
            await warp_proxy.warp.ensure_ready()
        except Exception as e:
            logging.warning(f"WARP check failed: {e}")
    return web.json_response(warp_proxy.warp.status())


async def handle_api_check_user(request):
    try:
        username = request.query.get("username", "").strip()
        if not username:
            return web.json_response({"found": False, "error": "empty_username"})
        user = find_user_by_username(username)
        if user:
            return web.json_response({
                "found": True,
                "user_id": user[0],
                "first_name": user[1]
            })
        return web.json_response({"found": False})
    except Exception as e:
        return web.json_response({"found": False, "error": str(e)}, status=500)


async def handle_api_send_invite(request):
    try:
        data = await request.json()
        sender_id = data.get("sender_id")
        sender_name = data.get("sender_name", "Тиммейт")
        sender_username = data.get("sender_username", "")
        recipient_username = data.get("recipient_username", "")
        place = data.get("place", "Roblox")
        day = data.get("day", "Сегодня")
        time = data.get("time", "20:00")
        vibe = data.get("vibe", "🍵 Уютный чилл")
        duration = data.get("duration", "1-2 часа")
        note = data.get("note", "")

        # Авто-регистрация отправителя в базе (если переданы данные из Telegram)
        if sender_id and str(sender_id) != "0":
            try:
                save_user(int(sender_id), sender_username, sender_name)
            except Exception as e:
                logging.warning(f"Error auto-saving sender: {e}")

        invite_id = create_invite_record(
            sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note
        )
        invite_link = f"https://t.me/duo_play_date_bot?start=inv_{invite_id}"
        app_link = get_app_url()

        recipient_db = find_user_by_username(recipient_username)
        if not recipient_db:
            return web.json_response({
                "success": True,
                "delivered": False,
                "invite_id": invite_id,
                "invite_link": invite_link,
                "reason": "not_in_bot",
                "message": f"Пользователь @{recipient_username} еще не нажал /start в боте"
            })

        target_chat_id, target_first_name = recipient_db

        invite_msg = (
            f"👾 **ТЕБЯ ЗОВУТ В ИГРУ В DUO!** 🎮\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 **От кого:** {sender_name}\n"
            f"🕹 **Игра:** {place}\n"
            f"📅 **Время:** {day} в {time}\n"
            f"✨ **Вайб:** {vibe}\n"
            f"⏳ **Сколько играем:** {duration}\n"
        )
        if note and note != "Без записки":
            invite_msg += f"💌 **Записка:** _{note}_\n"

        invite_msg += (
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Ты в деле? Выбери ответ ниже 👇"
        )

        reply_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Я в деле! Погнали!",
                        callback_data=f"accept_{sender_id or 0}_{invite_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏰ Давай, но позже",
                        callback_data=f"later_{sender_id or 0}_{invite_id}"
                    ),
                    InlineKeyboardButton(
                        text="💔 Не сегодня",
                        callback_data=f"decline_{sender_id or 0}_{invite_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎮 Открыть DUO ✨",
                        web_app=WebAppInfo(url=app_link)
                    )
                ]
            ]
        )

        if bot:
            try:
                await bot.send_message(
                    chat_id=target_chat_id,
                    text=invite_msg,
                    reply_markup=reply_kb,
                    parse_mode="Markdown"
                )
                return web.json_response({
                    "success": True,
                    "delivered": True,
                    "invite_id": invite_id,
                    "invite_link": invite_link,
                    "recipient_name": target_first_name
                })
            except Exception as send_err:
                logging.warning(f"Failed to send bot message to {target_chat_id}: {send_err}")
                return web.json_response({
                    "success": True,
                    "delivered": False,
                    "invite_id": invite_id,
                    "invite_link": invite_link,
                    "reason": "forbidden",
                    "message": f"Telegram не разрешил отправить сообщение: {send_err}"
                })
        else:
            return web.json_response({
                "success": True,
                "delivered": False,
                "invite_id": invite_id,
                "invite_link": invite_link,
                "reason": "no_bot",
                "message": "Бот не настроен на сервере"
            })

    except Exception as e:
        logging.error(f"Error in api_send_invite: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_api_logs(request):
    return web.json_response({
        "status": "ok",
        "recent_errors": recent_errors,
        "ffmpeg_path": str(FFMPEG_PATH),
        "ffmpeg_exists": os.path.exists(FFMPEG_PATH) if FFMPEG_PATH else False,
        "system_ffmpeg": shutil.which("ffmpeg"),
        "warp": warp_proxy.warp.status(),
    })


async def handle_api_test_dl(request):
    url = request.query.get("url", "").strip()
    if not url:
        return web.json_response({"error": "missing url query parameter"})
    fp = None
    try:
        res = await download_video(url, 9999)
        fp = res.get("file_path")
        sz = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
        return web.json_response({
            "status": "success",
            "title": res.get("title"),
            "platform": res.get("platform"),
            "size_bytes": sz,
            "warp": warp_proxy.warp.status(),
        })
    except Exception as e:
        import traceback
        return web.json_response({
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(),
            "warp": warp_proxy.warp.status(),
        }, status=500)
    finally:
        # Не оставляем тестовые файлы на диске Render.
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass


def create_web_app():
    app = web.Application()

    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=200)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as ex:
                resp = ex
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

    app.middlewares.append(cors_middleware)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stream/{media_key}", handle_stream_video)
    app.router.add_get("/api/logs", handle_api_logs)
    app.router.add_get("/api/warp", handle_api_warp)
    app.router.add_get("/api/test_dl", handle_api_test_dl)
    app.router.add_get("/api/check_user", handle_api_check_user)
    app.router.add_post("/api/send_invite", handle_api_send_invite)

    # Если включен режим Webhook и бот инициализирован — регистрируем aiogram webhook handler
    if bot and USE_WEBHOOK:
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
            handle_in_background=True,
            secret_token=WEBHOOK_SECRET,
        )
        webhook_requests_handler.register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        logging.info(f"🔗 Aiogram SimpleRequestHandler зарегистрирован на {WEBHOOK_PATH}")

    return app


async def warm_up_warp():
    """Фоновая подготовка WARP, чтобы первое YouTube-видео не ждало 5-10 сек."""
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


async def keep_alive_ping():
    """
    Каждые 9 минут отправляет внешний HTTP GET запрос на свой собственный /health endpoint.
    Это сбрасывает 15-минутный счетчик неактивности Render и не даёт серверу уснуть (24/7 бодрствование).
    """
    await asyncio.sleep(45)  # ждем полного старта сервера
    target_url = f"{RENDER_EXTERNAL_URL}/health"
    logging.info(f"🔄 Фоновый анти-сон запущен: цель {target_url}")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logging.info(f"⏰ Anti-sleep keepalive: HTTP {resp.status}")
        except Exception as e:
            logging.debug(f"Anti-sleep ping error: {e}")
        await asyncio.sleep(540)  # каждые 9 минут (Render засыпает через 15)


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ ВНИМАНИЕ: BOT_TOKEN не указан в .env файле!")
        print("Пожалуйста, получи токен у @BotFather и пропиши BOT_TOKEN=твой_токен")

    web_app = create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"🌐 HTTP API запущен на порту {PORT}")

    # Запускаем фоновый пингер анти-сна
    asyncio.create_task(keep_alive_ping())

    # Заранее поднимаем WARP для YouTube (не блокирует polling бота)
    asyncio.create_task(warm_up_warp())

    if bot:
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
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                await runner.cleanup()
                await bot.session.close()
        else:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                logging.info("🧹 Webhook снят для работы в режиме Polling")
            except Exception as e:
                logging.debug(f"delete_webhook error: {e}")
            logging.info("🤖 Бот запущен в режиме Polling (локально)...")
            try:
                await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            finally:
                await runner.cleanup()
                await bot.session.close()
    else:
        logging.warning("⚠️ Бот не запущен, так как нет BOT_TOKEN. HTTP сервер работает.")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()



if __name__ == "__main__":
    asyncio.run(main())
