"""
Административный модуль: проверка прав, загрузка cookies Instagram 18+,
защита отладочных API и криптографическая проверка Telegram WebApp initData (HMAC-SHA256).
"""

import os
import hmac
import html
import json
import time
import hashlib
import logging
import urllib.parse
from typing import Optional, Dict, Any

from aiogram import Router, F, types
from aiogram.filters import Command

from . import app as _app

logger = logging.getLogger(__name__)
router = Router(name="admin")


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 86400,
) -> Optional[Dict[str, Any]]:
    """
    Проверяет криптографическую подпись Telegram Mini App initData (HMAC-SHA256).
    Возвращает словарь распарсенных полей (включая 'user' dict) при валидной подписи,
    либо None при подделке/ошибке/устаревшем auth_date.
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    except Exception:
        return None

    data_map: Dict[str, str] = {}
    received_hash = None
    for k, v in pairs:
        if k == "hash":
            received_hash = v
        else:
            data_map[k] = v

    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_map.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    if max_age_seconds > 0 and "auth_date" in data_map:
        try:
            auth_ts = int(data_map["auth_date"])
            if abs(time.time() - auth_ts) > max_age_seconds:
                return None
        except (TypeError, ValueError):
            return None

    parsed: Dict[str, Any] = dict(data_map)
    if "user" in parsed and isinstance(parsed["user"], str):
        try:
            parsed["user"] = json.loads(parsed["user"])
        except Exception:
            pass
    return parsed


def is_bot_admin(user_id: int) -> bool:
    """Fail-closed: без ADMIN_ID / ORGANIZER_CHAT_ID админ-доступ запрещён всем."""
    a = _app()
    uid_str = str(user_id)
    if a.ADMIN_IDS and uid_str in a.ADMIN_IDS:
        return True
    if a.ORGANIZER_CHAT_ID and uid_str == str(a.ORGANIZER_CHAT_ID):
        return True
    return False


def admin_denied_text(user_id: int) -> str:
    return (
        "🔒 Эта команда доступна только администратору бота.\n\n"
        f"Твой Telegram ID: <code>{user_id}</code>\n"
        "Если бот твой — добавь <code>ADMIN_ID</code> с этим числом в Environment на Render."
    )


def debug_api_allowed(request) -> bool:
    """Доступ к /api/logs и /api/test_dl только по ?key=DEBUG_API_KEY."""
    a = _app()
    if not a.DEBUG_API_KEY:
        return False
    return hmac.compare_digest(request.query.get("key", ""), a.DEBUG_API_KEY)


@router.message(Command("cookies"))
async def handle_cookies_command(message: types.Message):
    a = _app()
    user_id = message.from_user.id if message.from_user else 0
    if not a.is_bot_admin(user_id):
        await message.answer(a.admin_denied_text(user_id), parse_mode="HTML")
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        cookie_val = parts[1].strip()
        if "=" not in cookie_val and "%3A" in cookie_val:
            u_id = cookie_val.split("%3A")[0]
            cookie_val = f"sessionid={cookie_val}; ds_user_id={u_id}"
        a.set_bot_setting("instagram_cookies", cookie_val)
        os.environ["INSTAGRAM_COOKIES"] = cookie_val
        try:
            from downloader import setup_cookies_file
            cf = setup_cookies_file("instagram")
        except Exception:
            cf = None

        await message.answer(
            "🍪 <b>Cookies Instagram успешно сохранены и активированы!</b>\n\n"
            f"✅ Файл сессии: <code>{html.escape(str(cf or 'instagram_cookies.txt'))}</code>\n"
            "✨ Теперь бот может скачивать ролики 18+ и закрытые посты без ограничений.\n\n"
            "<i>Чтобы удалить куки:</i> <code>/del_cookies</code>",
            parse_mode="HTML",
        )
        return

    from downloader import setup_cookies_file
    active_file = setup_cookies_file("instagram")
    db_val = a.get_bot_setting("instagram_cookies", "")

    if active_file or db_val or os.environ.get("INSTAGRAM_COOKIES") or os.environ.get("INSTAGRAM_SESSIONID"):
        src = (
            "База данных бота"
            if db_val
            else ("Переменные окружения" if (os.environ.get("INSTAGRAM_COOKIES") or os.environ.get("INSTAGRAM_SESSIONID")) else "Файл на диске")
        )
        await message.answer(
            f"🍪 <b>Статус Cookies Instagram:</b>\n"
            f"✅ <b>Активны</b> (Источник: {src})\n"
            f"📁 Файл: <code>{html.escape(str(active_file or 'instagram_cookies.txt'))}</code>\n\n"
            f"💡 <b>Как обновить:</b>\n"
            f"1. Отправь: <code>/cookies sessionid=...; ds_user_id=...</code>\n"
            f"2. Или просто скинь текстовый файл <code>cookies.txt</code> сюда в чат с подписью <code>/cookies</code>\n\n"
            f"❌ <i>Удалить:</i> <code>/del_cookies</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🍪 <b>Статус Cookies Instagram:</b>\n"
            "⚠️ <b>Не установлены</b> (ролики 18+ могут блокироваться Instagram)\n\n"
            "💡 <b>Как легко добавить куки без личного аккаунта:</b>\n"
            "1. Зарегистрируй любой пустой аккаунт в браузере (в режиме Инкогнито на временную почту, возраст 18+).\n"
            "2. Скопируй строку куки (<code>sessionid=...; ds_user_id=...</code>) или экспортируй файл <code>cookies.txt</code>.\n"
            "3. Отправь мне команду:\n"
            "<code>/cookies sessionid=ВАШ_SESSION_ID; ds_user_id=ВАШ_ID</code>\n"
            "или просто скинь файл <code>cookies.txt</code> сюда в чат!",
            parse_mode="HTML",
        )


@router.message(Command("del_cookies"))
async def handle_del_cookies_command(message: types.Message):
    a = _app()
    user_id = message.from_user.id if message.from_user else 0
    if not a.is_bot_admin(user_id):
        await message.answer(a.admin_denied_text(user_id), parse_mode="HTML")
        return

    a.set_bot_setting("instagram_cookies", "")
    os.environ.pop("INSTAGRAM_COOKIES", None)
    base_dir = os.path.dirname(os.path.dirname(__file__))
    for fn in ("instagram_cookies.txt", "cookies.txt"):
        p = os.path.join(base_dir, fn)
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    await message.answer(
        "🗑 <b>Cookies Instagram удалены.</b> Бот снова работает в чистом гостевом режиме.",
        parse_mode="HTML",
    )


@router.message(F.document)
async def handle_cookies_document(message: types.Message):
    a = _app()
    doc = message.document
    if not doc:
        return
    caption = (message.caption or "").strip()
    fn = (doc.file_name or "").lower()
    user_id = message.from_user.id if message.from_user else 0

    if not a.is_bot_admin(user_id):
        return

    if caption.startswith("/cookies") or "cookie" in fn:
        if doc.file_size and doc.file_size > 512 * 1024:
            await message.answer("⚠️ Файл слишком большой для файла cookies (лимит 512 КБ).")
            return
        try:
            file_info = await a.bot.get_file(doc.file_id)
            base_dir = os.path.dirname(os.path.dirname(__file__))
            dest = os.path.join(base_dir, "instagram_cookies.txt")
            await a.bot.download_file(file_info.file_path, dest)
            with open(dest, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            a.set_bot_setting("instagram_cookies", content)
            os.environ["INSTAGRAM_COOKIES"] = content
            await message.answer(
                "🍪 <b>Файл cookies успешно загружен и сохранен!</b>\n\n"
                f"✅ Сохранено в <code>instagram_cookies.txt</code> ({round(len(content)/1024, 1)} КБ).\n"
                "✨ Теперь ролики 18+ и закрытые посты будут скачиваться с этой сессией.",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка сохранения cookies: {e}")
