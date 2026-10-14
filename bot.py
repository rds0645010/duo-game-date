import os
import json
import sqlite3
import logging
import asyncio
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    MenuButtonWebApp
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ==========================================
# ⚙️ НАСТРОЙКИ (0 БЮДЖЕТА)
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rds0645010.github.io/duo-game-date/").strip()
ORGANIZER_CHAT_ID = os.getenv("ORGANIZER_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", 8080))
DB_PATH = "users.db"


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
    clean = username.lower().replace("@", "").strip()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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


init_db()

# Бот и Диспетчер
bot = Bot(token=BOT_TOKEN) if (BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE") else None
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject = None):
    user = message.from_user
    save_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name
    )

    welcome_text = (
        f"Привет, {user.first_name}! 👾\n\n"
        "Добро пожаловать в **DUO** — твой сервис для совместных игр и игровых свиданий!\n\n"
        "✨ Теперь твои друзья могут звать тебя в катки в один клик, а бот будет мгновенно присылать приглашения прямо сюда!\n\n"
        "Нажми кнопку ниже, чтобы открыть приложение и создать своё приглашение:"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎮 Открыть DUO ✨",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]
    )

    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="🎮 DUO",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
    except Exception as e:
        logging.warning(f"Menu button warning: {e}")

    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")


@dp.callback_query(F.data.startswith("accept_"))
async def handle_accept(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None
    
    await callback.answer("Приглашение принято! 🎉")
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ **Ты согласился(лась) пойти в игру! Запускай катку! 🚀**",
        parse_mode="Markdown"
    )

    if sender_id and bot:
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=f"🥳 **УРА!** {callback.from_user.full_name} принял(а) твоё приглашение в DUO! Погнали играть 🚀"
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


@dp.callback_query(F.data.startswith("later_"))
async def handle_later(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None

    await callback.answer("Ответ отправлен ⏰")
    await callback.message.edit_text(
        callback.message.text + "\n\n⏰ **Ответ:** Давай, но чуть попозже!",
        parse_mode="Markdown"
    )

    if sender_id and bot:
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=f"⏰ **{callback.from_user.full_name}** не против сыграть, но предлагает чуть позже!"
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


@dp.callback_query(F.data.startswith("decline_"))
async def handle_decline(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None

    await callback.answer("Отклонено 💔")
    await callback.message.edit_text(
        callback.message.text + "\n\n💔 **Ответ:** Сегодня не получится.",
        parse_mode="Markdown"
    )

    if sender_id and bot:
        try:
            await bot.send_message(
                chat_id=sender_id,
                text=f"💔 **{callback.from_user.full_name}** сегодня не сможет пойти в игру."
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


# ==========================================
# 🌐 ВСТРОЕННЫЙ HTTP API ДЛЯ САЙТА (MINI APP)
# ==========================================
async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "app": "DUO 24/7 Bot Server",
        "bot_configured": bool(bot)
    })


async def handle_api_send_invite(request):
    try:
        data = await request.json()
        sender_id = data.get("sender_id")
        sender_name = data.get("sender_name", "Тиммейт")
        recipient_username = data.get("recipient_username", "")
        place = data.get("place", "Roblox")
        day = data.get("day", "Сегодня")
        time = data.get("time", "20:00")
        vibe = data.get("vibe", "🍵 Уютный чилл")
        duration = data.get("duration", "1-2 часа")
        note = data.get("note", "")

        invite_id = create_invite_record(
            sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note
        )

        recipient_db = find_user_by_username(recipient_username)
        if not recipient_db:
            return web.json_response({
                "success": True,
                "delivered": False,
                "reason": "not_in_bot",
                "message": f"Пользователь @{recipient_username} еще не нажал /start в боте"
            })

        target_chat_id, target_first_name = recipient_db

        invite_msg = (
            f"💌 **ТЕБЯ ЗОВУТ В КАТКУ В DUO!** 🎮\n\n"
            f"От: **{sender_name}**\n\n"
            f"🕹 **Игра:** {place}\n"
            f"📅 **Время:** {day} в {time}\n"
            f"✨ **Вайб:** {vibe}\n"
            f"⏳ **Сколько играем:** {duration}\n"
        )
        if note and note != "Без записки":
            invite_msg += f"💌 **Записка:** _{note}_\n"

        invite_msg += "\nЧто скажешь? Погнали? 👇"

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
                ]
            ]
        )

        if bot:
            await bot.send_message(
                chat_id=target_chat_id,
                text=invite_msg,
                reply_markup=reply_kb,
                parse_mode="Markdown"
            )

        return web.json_response({
            "success": True,
            "delivered": True,
            "recipient_name": target_first_name
        })

    except Exception as e:
        logging.error(f"Error in api_send_invite: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


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
