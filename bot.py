import os
import json
import sqlite3
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    MenuButtonWebApp
)
import asyncio

logging.basicConfig(level=logging.INFO)

# ==========================================
# ⚙️ НАСТРОЙКИ (0 БЮДЖЕТА)
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rds0645010.github.io/duo-game-date/")
ORGANIZER_CHAT_ID = os.getenv("ORGANIZER_CHAT_ID", "")

# Бесплатная локальная база данных SQLite (0 рублей)
DB_PATH = "users.db"


def init_db():
    """Создание таблицы пользователей и истории приглашений."""
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
            recipient_username TEXT,
            place TEXT,
            date_time TEXT,
            duration TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def save_user(user_id: int, username: str, first_name: str, photo_url: str = None, gender: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, username, first_name, photo_url, gender)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            photo_url = COALESCE(excluded.photo_url, users.photo_url),
            gender = COALESCE(excluded.gender, users.gender),
            updated_at = CURRENT_TIMESTAMP
    """, (user_id, (username or "").lower().replace("@", ""), first_name, photo_url, gender))
    conn.commit()
    conn.close()


def find_user_by_username(username: str):
    if not username:
        return None
    clean_username = username.lower().replace("@", "").strip()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, first_name FROM users WHERE username = ?", (clean_username,))
    row = cursor.fetchone()
    conn.close()
    return row  # (user_id, first_name)


init_db()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Регистрация пользователя и кнопка открытия приложения."""
    user = message.from_user
    save_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name
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

    # Настраиваем меню-кнопку слева внизу
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

    await message.answer(
        f"Привет, {user.first_name}! 👾\n\n"
        "Добро пожаловать в **DUO** — сервис для совместных игр и игровых свиданий! "
        "Выбирай любую игру из своего списка, настраивай тайминг и зови тиммейта в пару кликов 🎮✨\n\n"
        "Нажми кнопку ниже, чтобы открыть приложение:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@dp.message(F.web_app_data)
async def handle_web_app_data(message: types.Message):
    """Обработка отправки приглашения из Mini App."""
    try:
        data = json.loads(message.web_app_data.data)

        sender = data.get("sender", {})
        recipient = data.get("recipient", {})
        place = data.get("place", "Roblox")
        game_tag = data.get("gameTag", "")
        day = data.get("day", "Сегодня")
        time = data.get("time", "20:00")
        vibe = data.get("vibe", "🍵 Уютный чилл")
        duration = data.get("duration", "1-2 часа")
        note = data.get("note", "")

        # Форматирование названия игры без дублирования
        clean_tag = game_tag.replace("🌐", "").replace("💻", "").replace("📱", "").replace("🎮", "").strip()
        if clean_tag and clean_tag.lower() not in ["кроссплатформа", ""] and place.lower() != clean_tag.lower():
            display_game = f"{place} · {game_tag}"
        else:
            display_game = place

        # Обновляем профиль отправителя с полом/стилем
        theme_gender = sender.get("gender") or sender.get("theme", "girl")
        save_user(
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            gender=theme_gender
        )

        sender_role = "👧 Девушка" if theme_gender == "girl" else "👦 Парень"
        recipient_name = recipient.get("name", "Друг")
        recipient_username = recipient.get("username", "")

        # 1. Ответ отправителю
        summary_for_sender = (
            "🎉 **Инвайт в DUO успешно создан!** 🎮\n\n"
            f"🎯 **Кому:** {recipient_name} ({recipient_username})\n"
            f"🕹 **Игра:** {display_game}\n"
            f"📅 **Когда:** {day} в {time}\n"
            f"✨ **Вайб:** {vibe}\n"
            f"⏳ **Длительность:** {duration}\n"
        )
        if note and note != "Без записки":
            summary_for_sender += f"💌 **Записка:** _{note}_\n"

        summary_for_sender += (
            "\n💡 *Если адресат еще не запускал бота, нажми кнопку 'Поделиться в Telegram' "
            "в приложении, чтобы отправить карточку прямо ему в чат!*"
        )

        await message.answer(summary_for_sender, parse_mode="Markdown")

        # 2. Проверяем, есть ли адресат в нашей базе данных пользователей бота
        recipient_db = find_user_by_username(recipient_username)
        if recipient_db:
            target_chat_id, target_first_name = recipient_db
            invite_for_recipient = (
                f"💌 **ТЕБЯ ЗОВУТ В КАТКУ В DUO!** 🎮\n\n"
                f"От: **{message.from_user.full_name}** ({sender_role})\n\n"
                f"🕹 **Игра:** {display_game}\n"
                f"📅 **Время:** {day} в {time}\n"
                f"✨ **Вайб:** {vibe}\n"
                f"⏳ **Сколько играем:** {duration}\n"
            )
            if note and note != "Без записки":
                invite_for_recipient += f"💌 **Записка:** _{note}_\n"

            invite_for_recipient += "\nЧто скажешь? Погнали? 👇"

            accept_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Я в деле! Погнали!",
                            callback_data=f"accept_{message.from_user.id}"
                        ),
                        InlineKeyboardButton(
                            text="⏰ Давай, но позже",
                            callback_data=f"later_{message.from_user.id}"
                        )
                    ]
                ]
            )

            try:
                await bot.send_message(
                    chat_id=target_chat_id,
                    text=invite_for_recipient,
                    reply_markup=accept_kb,
                    parse_mode="Markdown"
                )
                await message.answer(f"✅ Доставлено прямо в личные сообщения пользователю {recipient_name}!")
            except Exception as e:
                logging.warning(f"Не удалось отправить получателю: {e}")

        # 3. Уведомление организатору (если настроен ORGANIZER_CHAT_ID)
        if ORGANIZER_CHAT_ID and str(message.chat.id) != str(ORGANIZER_CHAT_ID):
            try:
                await bot.send_message(
                    chat_id=ORGANIZER_CHAT_ID,
                    text=f"🔔 [DUO Invite]\n{message.from_user.full_name} позвал {recipient_name} в {place} ({day} {time})!"
                )
            except Exception:
                pass

    except Exception as e:
        logging.error(f"Error handling web_app_data: {e}")
        await message.answer("Произошла ошибка при обработке приглашения.")


@dp.callback_query(F.data.startswith("accept_"))
async def handle_accept(callback: types.CallbackQuery):
    sender_id = int(callback.data.split("_")[1])
    await callback.answer("Приглашение принято! 🎉")
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ **Ты согласился(лась) пойти в игру!** 🎉",
        parse_mode="Markdown"
    )
    # Уведомляем отправителя
    try:
        await bot.send_message(
            chat_id=sender_id,
            text=f"🥳 **УРА!** {callback.from_user.full_name} принял(а) твоё приглашение! Запускай игру 🚀"
        )
    except Exception as e:
        logging.warning(f"Could not notify sender: {e}")
    except Exception as e:
        logging.warning(f"Could not notify sender: {e}")


@dp.callback_query(F.data.startswith("later_"))
async def handle_later(callback: types.CallbackQuery):
    sender_id = int(callback.data.split("_")[1])
    await callback.answer("Ответ отправлен")
    await callback.message.edit_text(
        callback.message.text + "\n\n⏰ **Ответ:** Давай, но чуть попозже!",
        parse_mode="Markdown"
    )
    try:
        await bot.send_message(
            chat_id=sender_id,
            text=f"⏰ {callback.from_user.full_name} хочет сыграть в Roblox, но предлагает чуть позже!"
        )
    except Exception as e:
        logging.warning(f"Could not notify sender: {e}")


async def main():
    print("🤖 Бот Roblox Date запущен! 0 бюджета, полная готовность.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
