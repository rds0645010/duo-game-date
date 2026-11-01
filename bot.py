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
    InputTextMessageContent,
    FSInputFile
)
from downloader import detect_platform, extract_url, download_video, parse_tiktok_tikwm, FFMPEG_PATH

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
            title TEXT,
            platform TEXT,
            author TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            inline_caption INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


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



def get_cached_video(url: str):
    if not url:
        return None
    clean_u = url.split("?")[0].strip().rstrip("/")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT file_id, title, platform, author FROM video_cache WHERE url = ?", (clean_u,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"file_id": row[0], "title": row[1], "platform": row[2], "author": row[3]}
    return None


def save_cached_video(url: str, file_id: str, title: str, platform: str, author: str = ""):
    if not url or not file_id:
        return
    clean_u = url.split("?")[0].strip().rstrip("/")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO video_cache (url, file_id, title, platform, author)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            file_id = excluded.file_id,
            title = excluded.title,
            platform = excluded.platform,
            author = excluded.author,
            created_at = CURRENT_TIMESTAMP
    """, (clean_u, file_id, title, platform, author))
    conn.commit()
    conn.close()


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


async def process_video_download(message: types.Message, url: str):
    clean_url = extract_url(url) or url
    platform = detect_platform(clean_url)
    if not platform:
        await message.answer("⚠️ Не удалось определить платформу. Поддерживаются: TikTok, YouTube Shorts, Reels.")
        return

    # 1. Проверяем кэш (если видео уже скачивалось ранее — отдаём МГНОВЕННО)
    cached = get_cached_video(clean_url)
    if cached and cached.get("file_id"):
        title_esc = html.escape(cached.get("title") or "Видео")
        caption = (
            f"🎬 <b>{title_esc}</b>\n\n"
            f"📱 Источник: {html.escape(cached.get('platform', 'Видео').capitalize())}\n"
            f"✨ <i>Скачано через @duo_play_date_bot без водяных знаков и рекламы (мгновенно из кэша ⚡)</i>"
        )
        try:
            await message.answer_video(
                video=cached["file_id"],
                caption=caption,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logging.warning(f"Failed to send cached file_id {cached['file_id']}: {e}")
            # Если Telegram удалил file_id или ошибка — скачиваем заново

    user_id = message.from_user.id
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
                parse_mode="HTML"
            )
        except Exception as html_send_err:
            logging.warning(f"HTML caption send error: {html_send_err}, sending plain caption...")
            sent = await message.answer_video(
                video=video_input,
                caption=f"🎬 {title_raw[:100]}\n✨ Скачано через @duo_play_date_bot"
            )

        # Сохраняем file_id в кэш для мгновенных ответов в будущем
        if sent and sent.video and sent.video.file_id:
            save_cached_video(
                clean_url,
                sent.video.file_id,
                res.get("title", ""),
                res.get("platform", ""),
                res.get("author", "")
            )

        # Удаляем сервисное сообщение со статусом
        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        record_error("process_video_download", e, clean_url)
        logging.error(f"Download processing failed for {clean_url}: {e}", exc_info=True)
        err_text = str(e)
        if "file_too_large" in err_text:
            msg = "⚠️ Видео превышает 50 МБ (лимит Telegram). Мы не можем его загрузить."
        elif "private" in err_text.lower() or "login" in err_text.lower():
            msg = "🔒 Это приватное видео или требует авторизации в сервисе."
        else:
            msg = f"❌ Не удалось скачать видео.\n\n⚠️ Ошибка: <code>{html.escape(str(e)[:150])}</code>"

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
        # Удаляем временный файл с диска
        if res and isinstance(res, dict) and res.get("file_path"):
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
            raw_b64 = command.args.replace("dl_", "").strip()
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += '=' * (4 - missing_padding)
            target_url = base64.b64decode(raw_b64).decode('utf-8')
            if detect_platform(target_url):
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


# ==========================================
# 📥 ИНЛАЙН РЕЖИМ (@duo_play_date_bot <ссылка>)
# ==========================================
@dp.inline_query()
async def inline_query_handler(inline_query: InlineQuery):
    query = inline_query.query.strip()

    if not query:
        results = [
            InlineQueryResultArticle(
                id="hint_empty",
                title="📥 Вставь ссылку на видео",
                description="TikTok (без вотермарки), YouTube Shorts или Instagram Reels",
                thumbnail_url="https://cdn-icons-png.flaticon.com/512/3074/3074767.png",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        "💡 **Как скачать видео без водяных знаков и рекламы:**\n\n"
                        "Введи `@duo_play_date_bot <ссылка>` в любом чате или отправь ссылку боту в личные сообщения!"
                    ),
                    parse_mode="Markdown"
                )
            )
        ]
        await inline_query.answer(results=results, cache_time=10, is_personal=True)
        return

    clean_url = extract_url(query)
    platform = detect_platform(clean_url) if clean_url else None

    if not platform or not clean_url:
        results = [
            InlineQueryResultArticle(
                id="hint_unsupported",
                title="⚠️ Ссылка не распознана",
                description="Поддерживаются: TikTok, YouTube Shorts, Instagram Reels",
                input_message_content=InputTextMessageContent(
                    message_text="⚠️ Отправьте прямую ссылку на TikTok, YouTube Shorts или Instagram Reels!"
                )
            )
        ]
        await inline_query.answer(results=results, cache_time=10, is_personal=True)
        return

    results = []
    user_id = inline_query.from_user.id
    caption_enabled = get_user_inline_caption(user_id)

    # 1. Проверяем кэш бота: если видео уже в кэше Telegram — отдаем файл мгновенно!
    cached = get_cached_video(clean_url)
    if cached and cached.get("file_id"):
        title_esc = html.escape(cached.get("title") or "Видео")
        full_caption = (
            f"🎬 <b>{title_esc}</b>\n\n"
            f"📱 {html.escape(cached.get('platform', 'Видео').capitalize())} | ✨ <i>Скачано через @duo_play_date_bot без рекламы</i>"
        )
        clean_res = InlineQueryResultCachedVideo(
            id=f"clean_{abs(hash(cached['file_id']))}",
            video_file_id=cached["file_id"],
            title="🎬 Чистое видео (без текста)",
            description=f"{cached['title'] or 'Видео'} • Без текста и подписей",
            caption=None
        )
        full_res = InlineQueryResultCachedVideo(
            id=f"full_{abs(hash(cached['file_id']))}",
            video_file_id=cached["file_id"],
            title=f"📝 {cached['title'] or 'Видео'}",
            description=f"С описанием: {cached.get('platform', '').capitalize()}",
            caption=full_caption,
            parse_mode="HTML"
        )
        if caption_enabled:
            results.extend([full_res, clean_res])
        else:
            results.extend([clean_res, full_res])

        await inline_query.answer(results=results, cache_time=5, is_personal=True)
        return

    # 2. Если TikTok — получаем прямой MP4 поток через быстрый TikWM API (< 1 сек)
    if platform == "tiktok":
        try:
            tikwm_data = await parse_tiktok_tikwm(clean_url)
            if tikwm_data and tikwm_data.get("video_url"):
                title_esc = html.escape(tikwm_data.get("title", "TikTok"))
                author_esc = html.escape(tikwm_data.get("author", "TikTok"))
                full_caption = (
                    f"🎬 <b>{title_esc}</b>\n\n"
                    f"👤 {author_esc} | ✨ <i>Без водяного знака через @duo_play_date_bot</i>"
                )
                cover_url = tikwm_data.get("cover") or "https://cdn-icons-png.flaticon.com/512/3046/3046121.png"
                clean_res = InlineQueryResultVideo(
                    id=f"tt_clean_{abs(hash(clean_url))}",
                    video_url=tikwm_data["video_url"],
                    mime_type="video/mp4",
                    thumbnail_url=cover_url,
                    title="🎬 Чистое видео (без текста)",
                    description="Только чистый видеоролик без подписи",
                    caption=None
                )
                full_res = InlineQueryResultVideo(
                    id=f"tt_full_{abs(hash(clean_url))}",
                    video_url=tikwm_data["video_url"],
                    mime_type="video/mp4",
                    thumbnail_url=cover_url,
                    title=f"📝 {tikwm_data.get('title') or 'TikTok'}",
                    description=f"👤 {tikwm_data.get('author', 'TikTok')} • С подписью",
                    caption=full_caption,
                    parse_mode="HTML"
                )
                if caption_enabled:
                    results.extend([full_res, clean_res])
                else:
                    results.extend([clean_res, full_res])

                await inline_query.answer(results=results, cache_time=5, is_personal=True)
                return
        except Exception as e:
            logging.warning(f"Inline TikWM parse error: {e}")

    # 3. Для YouTube Shorts, Instagram Reels (или если TikTok не закэширован):
    # Telegram Inline Query таймаутит через 2-3 секунды, поэтому отдаем интерактивную карточку
    # с кнопкой 1-клик перехода в бота, которая мгновенно запускает скачивание и сохраняет в кэш
    raw_b64 = base64.b64encode(clean_url.encode("utf-8")).decode("utf-8")
    dl_link = f"https://t.me/duo_play_date_bot?start=dl_{raw_b64}"

    plat_title = {
        "youtube": "YouTube Shorts",
        "instagram": "Instagram Reels",
        "tiktok": "TikTok"
    }.get(platform, "Видео")

    results.append(
        InlineQueryResultArticle(
            id=f"dl_{abs(hash(clean_url))}",
            title=f"📥 Скачать {plat_title} без рекламы",
            description="Нажми, чтобы отправить инвайт на скачивание чистого видео",
            thumbnail_url="https://cdn-icons-png.flaticon.com/512/727/727245.png",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🎬 **Чистое видео ({plat_title})**\n"
                    f"🔗 {clean_url}\n\n"
                    f"Нажми кнопку ниже, чтобы бот прислал готовое видео без водяных знаков 👇"
                ),
                parse_mode="Markdown"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🚀 Скачать чистое видео в ЛС",
                            url=dl_link
                        )
                    ]
                ]
            )
        )
    )
    await inline_query.answer(results=results, cache_time=30, is_personal=True)


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
    app.router.add_get("/api/logs", handle_api_logs)
    app.router.add_get("/api/test_dl", handle_api_test_dl)
    app.router.add_get("/api/check_user", handle_api_check_user)
    app.router.add_post("/api/send_invite", handle_api_send_invite)
    return app


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

    if bot:
        logging.info("🤖 Бот запущен и слушает Telegram (24/7)...")
        await dp.start_polling(bot)
    else:
        logging.warning("⚠️ Бот не запущен, так как нет BOT_TOKEN. HTTP сервер работает.")
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
