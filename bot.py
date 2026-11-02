import os
import json
import sqlite3
import logging
import asyncio
import base64
import html
import time
import shutil
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.client.session.aiohttp import AiohttpSession
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
    parse_instagram_embed
)

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

# 👑 ПРИОРИТЕТ И ОЧЕРЕДИ СКАЧИВАНИЯ
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS = set([x.strip() for x in ADMIN_ID_RAW.split(",") if x.strip()])

MAX_CONCURRENT_DOWNLOADS = 2
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
active_download_count = 0
waiting_queue_count = 0
queue_lock = asyncio.Lock()


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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


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


def save_dl_token(url: str) -> str:
    """Сохраняет URL для безопасного deep-link /start dl_{token} (до 64 символов)"""
    if not url:
        return ""
    import uuid
    token = str(uuid.uuid4()).replace("-", "")[:12]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO dl_tokens (token, url) VALUES (?, ?)", (token, url))
    conn.commit()
    conn.close()
    return token


def get_dl_token_url(token: str):
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM dl_tokens WHERE token = ?", (token.strip(),))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return None


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


def get_canonical_media_key(url: str) -> str:
    """Нормализует любые варианты ссылок одного и того же поста к общему ключу"""
    if not url:
        return ""
    import re
    u = url.strip()
    # 1. Instagram: ID шорткода (поддерживает /p/, /reel/, /reels/)
    m_ig = re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', u)
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


async def upload_media_to_telegram(file_path: str, is_photo: bool = False, user_id: int = None):
    """
    Загружает медиафайл в Telegram и возвращает file_id.
    Сначала пробует отправить в ЛС пользователю с disable_notification=True и сразу удалить.
    Если пользователь не открывал ЛС — пробует кандидатов из базы данных.
    """
    if not file_path or not os.path.exists(file_path):
        return None

    candidate_chats = []
    if user_id:
        candidate_chats.append(user_id)
    fallback = get_fallback_upload_chat()
    if fallback and fallback not in candidate_chats:
        candidate_chats.append(fallback)

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users ORDER BY created_at DESC LIMIT 5")
        for r in cursor.fetchall():
            if r[0] and int(r[0]) not in candidate_chats:
                candidate_chats.append(int(r[0]))
        conn.close()
    except Exception:
        pass

    for chat_id in candidate_chats:
        try:
            if is_photo:
                sent = await bot.send_photo(chat_id=chat_id, photo=FSInputFile(file_path), disable_notification=True)
                if sent and sent.photo:
                    f_id = sent.photo[-1].file_id
                    try:
                        await sent.delete()
                    except Exception:
                        pass
                    return f_id
            else:
                sent = await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), disable_notification=True)
                if sent and sent.video:
                    f_id = sent.video.file_id
                    try:
                        await sent.delete()
                    except Exception:
                        pass
                    return f_id
        except Exception as e:
            logging.debug(f"Upload attempt to {chat_id} failed: {e}")

    return None


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


RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip() or "https://duo-game-date.onrender.com"


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


def format_friendly_error(e: Exception) -> str:
    """Преобразует любые технические ошибки в понятные объяснения для пользователя."""
    err_text = str(e).lower()
    if "is_photo_post" in err_text or "no video in this post" in err_text:
        return (
            "📸 <b>В этой публикации находится фотография, а не видео.</b>\n\n"
            "Бот специализируется на загрузке видеороликов из <b>Reels, TikTok и YouTube Shorts</b>. "
            "Если автор опубликовал статичную картинку или фото-карусель, видео в ней отсутствует."
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
    elif "confirm you’re not a bot" in err_text or "confirm you're not a bot" in err_text:
        return (
            "🤖 <b>YouTube временно запросил проверку капчи для этого ролика.</b>\n\n"
            "Попробуй ещё раз через пару секунд или отправь другой Shorts/видео."
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



async def process_video_download(message: types.Message, url: str):
    clean_url = extract_url(url) or url
    platform = detect_platform(clean_url)
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

    global active_download_count, waiting_queue_count

    status_msg = None
    queue_acquired = False
    res = None

    try:
        # Проверяем занятость слотов
        if download_semaphore.locked() and not is_vip:
            async with queue_lock:
                waiting_queue_count += 1
                pos = waiting_queue_count
            status_msg = await message.answer(
                f"⏳ <b>В очереди на скачивание...</b>\n"
                f"Твоя позиция: {pos}. Ожидаем завершения предыдущих загрузок 🚀",
                parse_mode="HTML"
            )
        else:
            status_msg = await message.answer(
                "⚡ <b>Обрабатываю видео...</b>\n"
                "Скачиваем в максимальном качестве без водяных знаков...",
                parse_mode="HTML"
            )

        # VIP пользователи имеют безусловный приоритет
        if is_vip:
            queue_acquired = True
            await safe_edit_status(status_msg, "👑 <b>VIP приоритет активен!</b>\nСкачиваю видео вне очереди без ожидания...")
        else:
            await download_semaphore.acquire()
            queue_acquired = True
            async with queue_lock:
                if waiting_queue_count > 0:
                    waiting_queue_count -= 1
            await safe_edit_status(status_msg, "⚡ <b>Скачиваю видео...</b>\nОчищаю от водяных знаков и готовлю к отправке...")

        # Выполняем скачивание
        res = await download_video(clean_url, user_id)
        file_path = res.get("file_path")

        if not file_path or not os.path.exists(file_path):
            await safe_edit_status(status_msg, "❌ Не удалось загрузить видео файл. Возможно, видео удалено или автор ограничил доступ.")
            return

        # Проверка размера (Telegram Bot API лимит 50 МБ)
        file_size = os.path.getsize(file_path)
        if file_size > 50 * 1024 * 1024:
            await safe_edit_status(
                status_msg,
                f"⚠️ <b>Файл слишком большой ({round(file_size / (1024*1024), 1)} МБ)</b>\n\n"
                f"Telegram Bot API не позволяет отправлять ботам видео больше 50 МБ.\n"
                f"Попробуй более короткое видео или шортс."
            )
            return

        await safe_edit_status(status_msg, "📤 <b>Отправляю чистое видео в Telegram...</b>")

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

        # 🖼 Если это альбом / карусель из нескольких слайдов
        if res.get("is_album") and res.get("photos"):
            photos = res["photos"]
            album_caption = (
                f"📸 <b>{title_esc}</b>\n\n"
                f"📱 <b>Источник:</b> {plat_str} | 🖼 Слайдов: {len(photos)}\n"
                f"✨ <i>Листай слайды прямо в Telegram 👉</i>\n"
                f"✨ <i>Скачано через @duo_play_date_bot</i>"
            )
            media_group = []
            for i, p_path in enumerate(photos[:10]):
                p_file = FSInputFile(p_path)
                if i == 0:
                    media_group.append(InputMediaPhoto(media=p_file, caption=album_caption, parse_mode="HTML"))
                else:
                    media_group.append(InputMediaPhoto(media=p_file))

            sent_msgs = await message.answer_media_group(media=media_group)
            if sent_msgs and len(sent_msgs) > 0 and sent_msgs[0].photo:
                f_id = sent_msgs[0].photo[-1].file_id
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
        # Освобождаем семафор
        if queue_acquired and not is_vip:
            download_semaphore.release()
        # Удаляем временные файлы с диска
        if res and isinstance(res, dict):
            # Удаляем фото из альбома
            if res.get("photos"):
                for p in res["photos"]:
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            # Удаляем одиночный файл
            if res.get("file_path"):
                fp = res["file_path"]
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception as del_err:
                        logging.warning(f"Could not remove temp file {fp}: {del_err}")


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject = None):
    user = message.from_user
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
    is_enabled = get_user_inline_caption(user_id)
    btn_text = "🟢 Подпись: Включена" if is_enabled else "⚪ Подпись: Выключена"
    toggle_val = "0" if is_enabled else "1"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{btn_text} (нажми для смены)",
                    callback_data=f"toggle_caption_{toggle_val}"
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
    is_enabled = get_user_inline_caption(user_id)
    mode_descr = (
        "🟢 <b>Включена</b> — под видео будет автор, название и ссылка"
        if is_enabled else
        "⚪ <b>Выключена (по умолчанию)</b> — видео отправляется абсолютно чистым, без текста"
    )
    return (
        "⚙️ <b>НАСТРОЙКИ ЗАГРУЗЧИКА И DUO</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "💬 <b>Подпись к видео при отправке через @ в чатах:</b>\n\n"
        f"Текущий режим: {mode_descr}\n\n"
        "<i>Нажми на кнопку ниже, чтобы переключить режим:</i>"
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

async def pre_cache_video_to_disk(clean_url: str, token: str):
    """
    Фоновая предварительная загрузка видео на диск в temp_downloads,
    чтобы оно было готово к моменту запроса серверами Telegram.
    """
    if token in _stream_files and os.path.exists(_stream_files[token]):
        return
    lock = _stream_locks.setdefault(token, asyncio.Lock())
    async with lock:
        if token in _stream_files and os.path.exists(_stream_files[token]):
            return
        output_prefix = os.path.join(DOWNLOAD_DIR, f"dl_{token}")
        try:
            res = await download_video(clean_url, 0)
            if res and res.get("file_path") and os.path.exists(res["file_path"]):
                _stream_files[token] = res["file_path"]
        except Exception as e:
            logging.warning(f"Pre-cache error for {clean_url}: {e}")


async def handle_stream_video(request: web.Request) -> web.StreamResponse:
    """
    HTTP endpoint для отдачи видео напрямую серверам Telegram (RFC 7233 Range requests).
    Когда пользователь отправляет InlineQueryResultVideo в чат, Telegram скачивает MP4 отсюда.
    """
    media_key = request.match_info.get("media_key", "")
    token = media_key.replace(".mp4", "").strip()

    target_url = get_dl_token_url(token)
    if not target_url:
        return web.Response(text="Token not found or expired", status=404)

    file_path = _stream_files.get(token)
    if not file_path or not os.path.exists(file_path):
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(f"dl_{token}") and (fname.endswith(".mp4") or fname.endswith(".jpg")):
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

    # 1. Если запрос ПУСТОЙ (@duo_play_date_bot в чате с другом):
    # Показываем только одну чистую подсказку (без истории и без лишних кнопок)
    if not query:
        results = [
            InlineQueryResultArticle(
                id="hint_empty",
                title="📥 Вставь ссылку на видео или фото",
                description="TikTok, YouTube Shorts или Instagram Reels/Фото",
                thumbnail_url="https://cdn-icons-png.flaticon.com/512/3074/3074767.png",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        "💡 **Как отправить видео другу через @duo_play_date_bot:**\n\n"
                        "Вставь ссылку на TikTok, Shorts или Reels после имени бота:\n"
                        "`@duo_play_date_bot https://...`\n\n"
                        "И нажми на появившуюся кнопку, чтобы чистое видео улетело прямо в чат!"
                    ),
                    parse_mode="Markdown"
                )
            )
        ]
        await inline_query.answer(
            results=results,
            cache_time=5,
            is_personal=True
        )
        return

    clean_url = extract_url(query)
    platform = detect_platform(clean_url) if clean_url else None

    if not platform or not clean_url:
        results = [
            InlineQueryResultArticle(
                id="hint_unsupported",
                title="⚠️ Ссылка не распознана",
                description="Поддерживаются: TikTok, YouTube Shorts, Instagram Reels и фото",
                thumbnail_url="https://cdn-icons-png.flaticon.com/512/1000/1000966.png",
                input_message_content=InputTextMessageContent(
                    message_text="⚠️ Отправьте прямую ссылку на TikTok, YouTube Shorts или Instagram Reels!"
                )
            )
        ]
        await inline_query.answer(results=results, cache_time=10, is_personal=True)
        return

    # Названия и официальные иконки платформ для идеального визуального стиля (как в SavXBot)
    if platform == "instagram":
        plat_noun = "Reel"
        plat_source = "Instagram"
        plat_icon = "https://cdn-icons-png.flaticon.com/512/2111/2111463.png"
    elif platform == "tiktok":
        plat_noun = "видео"
        plat_source = "TikTok"
        plat_icon = "https://cdn-icons-png.flaticon.com/512/3046/3046121.png"
    elif platform == "youtube":
        plat_noun = "Shorts"
        plat_source = "YouTube"
        plat_icon = "https://cdn-icons-png.flaticon.com/512/1384/1384060.png"
    else:
        plat_noun = "видео"
        plat_source = "Видео"
        plat_icon = "https://cdn-icons-png.flaticon.com/512/3074/3074767.png"

    # 2. Проверяем кэш бота: если видео уже скачано ранее — отдаем файл МГНОВЕННО!
    cached = get_cached_media(clean_url)
    if cached and cached.get("file_id"):
        title_esc = html.escape(cached.get("title") or plat_noun)
        m_type = cached.get("media_type", "video")

        if caption_enabled:
            if m_type == "photo":
                caption = (
                    f"📸 <b>{title_esc}</b>\n\n"
                    f"📱 {plat_source} | ✨ <i>Скачано через @duo_play_date_bot</i>"
                )
            else:
                caption = (
                    f"🎬 <b>{title_esc}</b>\n\n"
                    f"📱 {plat_source} | ✨ <i>Скачано через @duo_play_date_bot без рекламы</i>"
                )
        else:
            caption = None

        if m_type == "photo":
            single_result = InlineQueryResultCachedPhoto(
                id=f"c_p_{abs(hash(cached['file_id']))}",
                photo_file_id=cached["file_id"],
                title=f"💌 Поделиться фото из {plat_source}",
                caption=caption,
                parse_mode="HTML" if caption else None
            )
        else:
            single_result = InlineQueryResultCachedVideo(
                id=f"c_v_{abs(hash(cached['file_id']))}",
                video_file_id=cached["file_id"],
                title=f"💌 Поделиться {plat_noun} из {plat_source}",
                caption=caption,
                parse_mode="HTML" if caption else None
            )

        # Отдаем РОВНО ОДИН результат без switch_pm_text (стиль SavXBot)
        await inline_query.answer(
            results=[single_result],
            cache_time=300,
            is_personal=True
        )
        return

    # 3. Для TikTok: быстрый прямой поток без водяного знака через TikWM API
    if platform == "tiktok":
        direct_info = await parse_tiktok_tikwm(clean_url)
        if direct_info and direct_info.get("video_url"):
            title_esc = html.escape(direct_info.get("title") or "TikTok видео")
            caption = (
                f"🎬 <b>{title_esc}</b>\n\n📱 TikTok | ✨ <i>Скачано через @duo_play_date_bot</i>"
                if caption_enabled else None
            )
            single_result = InlineQueryResultVideo(
                id=f"dir_tt_{abs(hash(clean_url))}",
                video_url=direct_info["video_url"],
                mime_type="video/mp4",
                thumbnail_url=plat_icon,
                title=f"💌 Поделиться {plat_noun} из {plat_source}",
                caption=caption,
                parse_mode="HTML" if caption else None
            )
            await inline_query.answer(
                results=[single_result],
                cache_time=120,
                is_personal=True
            )
            return

    # 4. Скачиваем ролик на лету и загружаем в Telegram (стиль SavXBot, ~4-5 секунд)
    try:
        res = await asyncio.wait_for(download_video(clean_url, user_id), timeout=6.5)
        if res and res.get("file_path"):
            is_photo = bool(res.get("is_photo"))
            f_id = await upload_media_to_telegram(res["file_path"], is_photo=is_photo, user_id=user_id)
            if f_id:
                save_cached_media(
                    clean_url,
                    f_id,
                    "photo" if is_photo else "video",
                    res.get("title", ""),
                    res.get("platform", ""),
                    res.get("author", "")
                )
                title_esc = html.escape(res.get("title") or plat_noun)
                if caption_enabled:
                    caption = (
                        f"📸 <b>{title_esc}</b>\n\n📱 {plat_source} | ✨ <i>Скачано через @duo_play_date_bot</i>"
                        if is_photo else
                        f"🎬 <b>{title_esc}</b>\n\n📱 {plat_source} | ✨ <i>Скачано через @duo_play_date_bot</i>"
                    )
                else:
                    caption = None

                if is_photo:
                    single_result = InlineQueryResultCachedPhoto(
                        id=f"live_p_{abs(hash(f_id))}",
                        photo_file_id=f_id,
                        title=f"💌 Поделиться фото из {plat_source}",
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )
                else:
                    single_result = InlineQueryResultCachedVideo(
                        id=f"live_v_{abs(hash(f_id))}",
                        video_file_id=f_id,
                        title=f"💌 Поделиться {plat_noun} из {plat_source}",
                        caption=caption,
                        parse_mode="HTML" if caption else None
                    )

                await inline_query.answer(
                    results=[single_result],
                    cache_time=300,
                    is_personal=True
                )
                return
    except asyncio.TimeoutError:
        logging.info(f"Inline prefetch timeout for {clean_url}")
    except Exception as e:
        logging.warning(f"Inline prefetch error for {clean_url}: {e}")

    # Если ролик скачивается чуть дольше — отдаем карточку ожидания с cache_time=1 (Telegram тут же обновит)
    token = save_dl_token(clean_url)
    results = [
        InlineQueryResultArticle(
            id=f"wait_{token}",
            title=f"💌 Поделиться {plat_noun} из {plat_source}",
            description="⏳ Видео обрабатывается... Нажмите через 2-3 секунды",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/3074/3074767.png",
            input_message_content=InputTextMessageContent(
                message_text=clean_url
            )
        )
    ]
    try:
        await inline_query.answer(results=results, cache_time=1, is_personal=True)
    except Exception as ans_err:
        logging.error(f"Error answering inline query: {ans_err}")


# ==========================================
# 💬 ОБРАБОТЧИК СООБЩЕНИЙ В ЛС БОТА
# ==========================================
@dp.message(F.text)
async def handle_text_messages(message: types.Message):
    # Проверяем наличие ссылок на видео
    url = extract_url(message.text)
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
    return web.json_response({
        "status": "ok",
        "app": "DUO 24/7 Bot Server",
        "bot_configured": bool(bot)
    })


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
        "system_ffmpeg": shutil.which("ffmpeg")
    })


async def handle_api_test_dl(request):
    url = request.query.get("url", "").strip()
    if not url:
        return web.json_response({"error": "missing url query parameter"})
    try:
        res = await download_video(url, 9999)
        fp = res.get("file_path")
        sz = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
        return web.json_response({
            "status": "success",
            "title": res.get("title"),
            "platform": res.get("platform"),
            "file_path": fp,
            "size_bytes": sz
        })
    except Exception as e:
        import traceback
        return web.json_response({
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }, status=500)


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
    app.router.add_get("/api/test_dl", handle_api_test_dl)
    app.router.add_get("/api/check_user", handle_api_check_user)
    app.router.add_post("/api/send_invite", handle_api_send_invite)
    return app


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

    if bot:
        logging.info("🤖 Бот запущен и слушает Telegram (24/7)...")
        await dp.start_polling(bot)
    else:
        logging.warning("⚠️ Бот не запущен, так как нет BOT_TOKEN. HTTP сервер работает.")
        while True:
            await asyncio.sleep(3600)



if __name__ == "__main__":
    asyncio.run(main())
