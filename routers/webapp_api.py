"""
Модуль WebApp (DUO Mini App), команд /start, /settings, инвайтов
и HTTP-сервера (healthcheck, стриминг, диагностика, API с проверкой Telegram initData).
"""

import os
import base64
import shutil
import asyncio
import logging

from aiohttp import web
from aiogram import Router, F, types
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import warp_proxy
from downloader import detect_platform, download_video, FFMPEG_PATH, DOWNLOAD_DIR
from . import app as _app
from .admin import verify_telegram_init_data

logger = logging.getLogger(__name__)
router = Router(name="webapp_api")


@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject = None):
    a = _app()
    user = message.from_user
    logging.info(f"🚀 [КОМАНДА /start] от {user.id} (@{user.username or user.first_name}) args={command.args if command else None}")
    a.save_user(user_id=user.id, username=user.username, first_name=user.first_name)

    app_link = a.get_app_url()

    if command and command.args and command.args.startswith("dl_"):
        try:
            token_or_b64 = command.args.replace("dl_", "").strip()
            target_url = a.get_dl_token_url(token_or_b64)
            if not target_url:
                try:
                    raw_b64 = token_or_b64
                    missing_padding = len(raw_b64) % 4
                    if missing_padding:
                        raw_b64 += "=" * (4 - missing_padding)
                    target_url = base64.b64decode(raw_b64).decode("utf-8")
                except Exception:
                    target_url = None

            if target_url and detect_platform(target_url):
                await a.process_video_download(message, target_url)
                return
        except Exception as e:
            logging.error(f"Error handling dl_ start: {e}")

    if command and command.args and command.args.startswith("inv_"):
        try:
            inv_id_str = command.args.replace("inv_", "").strip()
            if inv_id_str.isdigit():
                inv = a.get_invite_record(int(inv_id_str))
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
                    invite_msg += "━━━━━━━━━━━━━━━━━━━\nТы в деле? Выбери ответ ниже 👇"

                    reply_kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Я в деле! Погнали!", callback_data=f"accept_{inv['sender_id'] or 0}_{inv['id']}")],
                            [
                                InlineKeyboardButton(text="⏰ Давай, но позже", callback_data=f"later_{inv['sender_id'] or 0}_{inv['id']}"),
                                InlineKeyboardButton(text="💔 Не сегодня", callback_data=f"decline_{inv['sender_id'] or 0}_{inv['id']}"),
                            ],
                            [InlineKeyboardButton(text="🎮 Открыть DUO ✨", web_app=WebAppInfo(url=app_link))],
                        ]
                    )
                    await message.answer(invite_msg, reply_markup=reply_kb, parse_mode="Markdown")
                    return
        except Exception as e:
            logging.error(f"Error handling deep link invite: {e}")

    welcome_text = (
        f"👾 **DUO — ИГРЫ И ЗАГРУЗЧИК МЕДИА БЕЗ РЕКЛАМЫ** 🎮\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Привет, {user.first_name}! 👋\n\n"
        f"✨ **Совместные игры в DUO:**\n"
        f"• Зови тиммейтов в любимые игры за 2 клика\n"
        f"• Назначай тайминги и собирай дуэты\n\n"
        f"📥 **Загрузка видео и музыки:**\n"
        f"• Отправь ссылку на **TikTok, Shorts, Reels, Spotify или SoundCloud**\n"
        f"• Для поиска любого трека напиши: `/music <название>`\n"
        f"• В чатах с друзьями пиши: `@duo_play_date_bot <ссылка>`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Открой лобби игр ниже или скинь ссылку прямо в чат 👇"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Открыть лобби DUO ✨", web_app=WebAppInfo(url=app_link))],
            [
                InlineKeyboardButton(text="🎵 Найти музыку (/music)", callback_data="btn_music_search"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="open_settings"),
            ],
        ]
    )

    try:
        await a.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="🎮 DUO", web_app=WebAppInfo(url=app_link)),
        )
    except Exception as e:
        logging.warning(f"Menu button warning: {e}")

    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")


def get_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    a = _app()
    is_caption = a.get_user_inline_caption(user_id)
    is_pm_save = a.get_user_save_to_pm(user_id)
    cap_btn = "🟢 Подпись: Включена" if is_caption else "⚪ Подпись: Выключена"
    toggle_cap = "0" if is_caption else "1"
    pm_btn = "🟢 Архив в ЛС: Включен" if is_pm_save else "⚪ Архив в ЛС: Выключен (чистая личка)"
    toggle_pm = "0" if is_pm_save else "1"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{cap_btn} (сменить)", callback_data=f"toggle_caption_{toggle_cap}")],
            [InlineKeyboardButton(text=f"{pm_btn} (сменить)", callback_data=f"toggle_pm_{toggle_pm}")],
            [InlineKeyboardButton(text="🎮 Открыть лобби DUO ✨", web_app=WebAppInfo(url=a.get_app_url()))],
        ]
    )


def get_settings_text(user_id: int) -> str:
    a = _app()
    is_caption = a.get_user_inline_caption(user_id)
    is_pm_save = a.get_user_save_to_pm(user_id)
    mode_descr = (
        "🟢 <b>Включена (по умолчанию)</b> — под видео добавляется скрытое описание и музыкальный трек"
        if is_caption
        else "⚪ <b>Выключена</b> — видео отправляется абсолютно чистым, без текста"
    )
    pm_descr = (
        "🟢 <b>Включен</b> — копии отправленных через @ видео сохраняются в вашей переписке с ботом"
        if is_pm_save
        else "⚪ <b>Выключен (по умолчанию)</b> — личка бота остаётся чистой, сообщения мгновенно удаляются после отправки другу"
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


@router.message(Command("settings"))
@router.message(F.text == "/settings")
@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    user_id = message.from_user.id
    await message.answer(
        get_settings_text(user_id),
        reply_markup=get_settings_keyboard(user_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "open_settings")
async def handle_open_settings(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    try:
        await callback.message.edit_text(
            get_settings_text(user_id),
            reply_markup=get_settings_keyboard(user_id),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            get_settings_text(user_id),
            reply_markup=get_settings_keyboard(user_id),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("toggle_caption_"))
async def callback_toggle_caption(callback: types.CallbackQuery):
    a = _app()
    new_val = callback.data.replace("toggle_caption_", "") == "1"
    a.set_user_inline_caption(callback.from_user.id, new_val)
    tip = "🟢 Подпись к видео включена!" if new_val else "⚪ Чистый режим активирован (без текста)!"
    await callback.answer(tip)
    try:
        await callback.message.edit_text(
            get_settings_text(callback.from_user.id),
            reply_markup=get_settings_keyboard(callback.from_user.id),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("toggle_pm_"))
async def callback_toggle_pm(callback: types.CallbackQuery):
    a = _app()
    new_val = callback.data.replace("toggle_pm_", "") == "1"
    a.set_user_save_to_pm(callback.from_user.id, new_val)
    tip = "🟢 Архив в ЛС включен!" if new_val else "⚪ Личка будет чистой (видео не сохраняются в ЛС)!"
    await callback.answer(tip)
    try:
        await callback.message.edit_text(
            get_settings_text(callback.from_user.id),
            reply_markup=get_settings_keyboard(callback.from_user.id),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("accept_"))
async def handle_accept(callback: types.CallbackQuery):
    a = _app()
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None
    await callback.answer("Приглашение принято! 🎉")
    await callback.message.edit_text(
        callback.message.text
        + "\n\n━━━━━━━━━━━━━━━━━━━\n✅ **Ты в деле! Запускай игру! 🚀**\nТвой напарник уже получил уведомление.",
        parse_mode="Markdown",
    )
    if sender_id and a.bot:
        try:
            await a.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"🎉 **ЕСТЬ КОНТАКТ! ТИММЕЙТ В ДЕЛЕ!** 🚀\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 **{callback.from_user.full_name}** принял(а) твой инвайт в DUO!\n"
                    f"Готовьтесь к совместной кате 🎮✨"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


@router.callback_query(F.data.startswith("later_"))
async def handle_later(callback: types.CallbackQuery):
    a = _app()
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None
    await callback.answer("Ответ отправлен ⏰")
    await callback.message.edit_text(
        callback.message.text + "\n\n━━━━━━━━━━━━━━━━━━━\n⏰ **Ответ:** Давай, но чуть попозже!",
        parse_mode="Markdown",
    )
    if sender_id and a.bot:
        try:
            await a.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"⏰ **ОТВЕТ ОТ НАПАРНИКА**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 **{callback.from_user.full_name}** не против сыграть, но предлагает чуть позже!"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


@router.callback_query(F.data.startswith("decline_"))
async def handle_decline(callback: types.CallbackQuery):
    a = _app()
    parts = callback.data.split("_")
    sender_id = int(parts[1]) if len(parts) > 1 and parts[1] != "0" else None
    await callback.answer("Отклонено 💔")
    await callback.message.edit_text(
        callback.message.text + "\n\n━━━━━━━━━━━━━━━━━━━\n💔 **Ответ:** Сегодня не получится.",
        parse_mode="Markdown",
    )
    if sender_id and a.bot:
        try:
            await a.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"💔 **ОТВЕТ ОТ НАПАРНИКА**\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 **{callback.from_user.full_name}** сегодня не сможет пойти в игру."
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.warning(f"Could not notify sender: {e}")


# ───────────────────────────────────────────────────────────────────────────
# 🌐 HTTP Endpoints (Health, WARP, Streaming, WebApp API с initData)
# ───────────────────────────────────────────────────────────────────────────

async def expire_stream_file(token: str, path: str, delay: int = 900):
    a = _app()
    try:
        await asyncio.sleep(delay)
        if a._stream_files.get(token) != path:
            return
        a._stream_files.pop(token, None)
        if os.path.exists(path):
            os.remove(path)
            logging.info(f"Pre-cache cleanup: removed {path}")
    except Exception:
        pass


async def pre_cache_video_to_disk(clean_url: str, token: str):
    a = _app()
    if token in a._stream_files and os.path.exists(a._stream_files[token]):
        return
    for stale_token in [k for k, v in a._stream_files.items() if not os.path.exists(v)]:
        a._stream_files.pop(stale_token, None)
    if len(a._stream_files) >= 5:
        return
    lock = a._stream_locks.setdefault(token, asyncio.Lock())
    async with lock:
        if token in a._stream_files and os.path.exists(a._stream_files[token]):
            return
        async with a._precache_semaphore:
            if token in a._stream_files and os.path.exists(a._stream_files[token]):
                return
            if len(a._stream_files) >= 5:
                return
            try:
                res = await download_video(clean_url, 0)
                if res and res.get("file_path") and os.path.exists(res["file_path"]):
                    a._stream_files[token] = res["file_path"]
                    asyncio.create_task(expire_stream_file(token, res["file_path"]))
            except Exception as e:
                logging.warning(f"Pre-cache error for {clean_url}: {e}")


async def handle_stream_video(request: web.Request) -> web.StreamResponse:
    a = _app()
    media_key = request.match_info.get("media_key", "")
    token = media_key.replace(".mp4", "").replace(".jpg", "").strip()

    target_url, db_file_path = a.get_dl_token_info(token)
    if not target_url and not db_file_path:
        return web.Response(text="Token not found or expired", status=404)

    file_path = a._stream_files.get(token) or db_file_path
    if not file_path or not os.path.exists(file_path):
        for fname in os.listdir(DOWNLOAD_DIR):
            if token in fname and (fname.endswith(".mp4") or fname.endswith(".jpg")):
                cand = os.path.join(DOWNLOAD_DIR, fname)
                if os.path.exists(cand) and os.path.getsize(cand) > 500:
                    file_path = cand
                    a._stream_files[token] = file_path
                    break

    if not file_path or not os.path.exists(file_path):
        lock = a._stream_locks.setdefault(token, asyncio.Lock())
        async with lock:
            file_path = a._stream_files.get(token)
            if not file_path or not os.path.exists(file_path):
                try:
                    res = await download_video(target_url, 0)
                    if res and res.get("file_path") and os.path.exists(res["file_path"]):
                        file_path = res["file_path"]
                        a._stream_files[token] = file_path
                        a.update_dl_token_file(token, file_path)
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
                "Cache-Control": "public, max-age=86400",
            },
        )
    return web.Response(text="File not found", status=404)


async def handle_health(request):
    a = _app()
    warp_status = warp_proxy.warp.status()
    return web.json_response({
        "status": "ok",
        "app": "DUO 24/7 Bot Server",
        "bot_configured": bool(a.bot),
        "mode": "webhook" if a.USE_WEBHOOK else "polling",
        "webhook_url": f"{a.RENDER_EXTERNAL_URL}{a.WEBHOOK_PATH}" if a.USE_WEBHOOK else None,
        "warp_enabled": warp_status["enabled"],
        "warp_ready": warp_status["ready"],
    })


async def handle_api_warp(request):
    if request.query.get("check", "").lower() in ("1", "true", "yes", "on"):
        try:
            await warp_proxy.warp.ensure_ready()
        except Exception as e:
            logging.warning(f"WARP check failed: {e}")
    return web.json_response(warp_proxy.warp.status())


def _extract_init_data(request, body_data: dict = None) -> str:
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not raw and body_data and isinstance(body_data, dict):
        raw = str(body_data.get("init_data") or "").strip()
    if not raw:
        raw = str(request.query.get("init_data") or "").strip()
    return raw


async def handle_api_check_user(request):
    a = _app()
    try:
        raw_init = _extract_init_data(request)
        if raw_init and a.BOT_TOKEN:
            if not verify_telegram_init_data(raw_init, a.BOT_TOKEN):
                return web.json_response({"found": False, "error": "invalid_init_data"}, status=403)

        username = request.query.get("username", "").strip()
        if not username:
            return web.json_response({"found": False, "error": "empty_username"})
        user = a.find_user_by_username(username)
        if user:
            return web.json_response({"found": True, "user_id": user[0], "first_name": user[1]})
        return web.json_response({"found": False})
    except Exception as e:
        return web.json_response({"found": False, "error": str(e)}, status=500)


async def handle_api_send_invite(request):
    a = _app()
    try:
        data = await request.json()
        raw_init = _extract_init_data(request, data)
        verified = None
        if raw_init and a.BOT_TOKEN:
            verified = verify_telegram_init_data(raw_init, a.BOT_TOKEN)
            if not verified:
                return web.json_response({"success": False, "error": "invalid_init_data"}, status=403)

        sender_id = data.get("sender_id")
        sender_name = data.get("sender_name", "Тиммейт")
        sender_username = data.get("sender_username", "")
        if verified and isinstance(verified.get("user"), dict):
            v_user = verified["user"]
            sender_id = v_user.get("id", sender_id)
            sender_username = v_user.get("username") or sender_username
            if not data.get("sender_name"):
                sender_name = v_user.get("first_name") or sender_name

        recipient_username = data.get("recipient_username", "")
        place = data.get("place", "Roblox")
        day = data.get("day", "Сегодня")
        time_str = data.get("time", "20:00")
        vibe = data.get("vibe", "🍵 Уютный чилл")
        duration = data.get("duration", "1-2 часа")
        note = data.get("note", "")

        if sender_id and str(sender_id) != "0":
            try:
                a.save_user(int(sender_id), sender_username, sender_name)
            except Exception as e:
                logging.warning(f"Error auto-saving sender: {e}")

        invite_id = a.create_invite_record(
            sender_id, sender_name, recipient_username, place, day, time_str, vibe, duration, note
        )
        invite_link = f"https://t.me/duo_play_date_bot?start=inv_{invite_id}"
        app_link = a.get_app_url()

        recipient_db = a.find_user_by_username(recipient_username)
        if not recipient_db:
            return web.json_response({
                "success": True,
                "delivered": False,
                "invite_id": invite_id,
                "invite_link": invite_link,
                "reason": "not_in_bot",
                "message": f"Пользователь @{recipient_username} еще не нажал /start в боте",
            })

        target_chat_id, target_first_name = recipient_db
        invite_msg = (
            f"👾 **ТЕБЯ ЗОВУТ В ИГРУ В DUO!** 🎮\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 **От кого:** {sender_name}\n"
            f"🕹 **Игра:** {place}\n"
            f"📅 **Время:** {day} в {time_str}\n"
            f"✨ **Вайб:** {vibe}\n"
            f"⏳ **Сколько играем:** {duration}\n"
        )
        if note and note != "Без записки":
            invite_msg += f"💌 **Записка:** _{note}_\n"
        invite_msg += "━━━━━━━━━━━━━━━━━━━\nТы в деле? Выбери ответ ниже 👇"

        reply_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Я в деле! Погнали!", callback_data=f"accept_{sender_id or 0}_{invite_id}")],
                [
                    InlineKeyboardButton(text="⏰ Давай, но позже", callback_data=f"later_{sender_id or 0}_{invite_id}"),
                    InlineKeyboardButton(text="💔 Не сегодня", callback_data=f"decline_{sender_id or 0}_{invite_id}"),
                ],
                [InlineKeyboardButton(text="🎮 Открыть DUO ✨", web_app=WebAppInfo(url=app_link))],
            ]
        )

        if a.bot:
            try:
                await a.bot.send_message(
                    chat_id=target_chat_id,
                    text=invite_msg,
                    reply_markup=reply_kb,
                    parse_mode="Markdown",
                )
                return web.json_response({
                    "success": True,
                    "delivered": True,
                    "invite_id": invite_id,
                    "invite_link": invite_link,
                    "recipient_name": target_first_name,
                })
            except Exception as send_err:
                logging.warning(f"Failed to send bot message to {target_chat_id}: {send_err}")
                return web.json_response({
                    "success": True,
                    "delivered": False,
                    "invite_id": invite_id,
                    "invite_link": invite_link,
                    "reason": "forbidden",
                    "message": f"Telegram не разрешил отправить сообщение: {send_err}",
                })
        return web.json_response({
            "success": True,
            "delivered": False,
            "invite_id": invite_id,
            "invite_link": invite_link,
            "reason": "no_bot",
            "message": "Бот не настроен на сервере",
        })
    except Exception as e:
        logging.error(f"Error in api_send_invite: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_api_logs(request):
    a = _app()
    if not a._debug_api_allowed(request):
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response({
        "status": "ok",
        "recent_errors": a.recent_errors,
        "ffmpeg_path": str(FFMPEG_PATH),
        "ffmpeg_exists": os.path.exists(FFMPEG_PATH) if FFMPEG_PATH else False,
        "system_ffmpeg": shutil.which("ffmpeg"),
        "warp": warp_proxy.warp.status(),
    })


async def handle_api_test_dl(request):
    a = _app()
    if not a._debug_api_allowed(request):
        return web.json_response({"error": "forbidden"}, status=403)
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
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass


def create_web_app():
    a = _app()
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
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Telegram-Init-Data"
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

    if a.bot and a.USE_WEBHOOK:
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=a.dp,
            bot=a.bot,
            handle_in_background=True,
            secret_token=a.WEBHOOK_SECRET,
        )
        webhook_requests_handler.register(app, path=a.WEBHOOK_PATH)
        setup_application(app, a.dp, bot=a.bot)

    return app
