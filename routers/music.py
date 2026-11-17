"""
Модуль поиска и скачивания музыки (/music, Spotify, YouTube Music, SoundCloud).
"""

import os
import html
import uuid
import logging

from aiogram import Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

import music_service
import warp_proxy
from downloader import DOWNLOAD_DIR
from . import app as _app

logger = logging.getLogger(__name__)
router = Router(name="music")


@router.callback_query(F.data == "btn_music_search")
async def handle_btn_music_search(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        "🎵 <b>Поиск и скачивание музыки:</b>\n\n"
        "Чтобы найти трек, напиши команду <code>/music</code> и название или автора:\n\n"
        "• <code>/music VLDONE Не пробил</code>\n"
        "• <code>/music Linkin Park Numb</code>\n"
        "• <code>/music Instasamka</code>\n\n"
        "Либо просто <b>отправь ссылку</b> на трек из <b>Spotify</b> или <b>SoundCloud</b> прямо в этот чат! ✨\n\n"
        "<i>(Нажми на любой пример выше, чтобы быстро скопировать команду)</i>"
    )
    await callback.message.answer(text, parse_mode="HTML")


@router.message(Command("music", "song", "track", "audio", "музыка", "трек", "песня"))
async def cmd_music_search(message: types.Message, command: CommandObject = None):
    a = _app()
    query = (command.args or "").strip() if command else ""
    if not query:
        await message.answer(
            "🎵 <b>Поиск и скачивание музыки:</b>\n\n"
            "Напиши название трека или имя исполнителя после команды, например:\n"
            "<code>/music Linkin Park Numb</code>\n"
            "<code>/music Queen Bohemian Rhapsody</code>\n"
            "<code>/music Instasamka</code>\n\n"
            "Или просто отправь ссылку на трек из <b>Spotify</b> или <b>SoundCloud</b> прямо в чат! ✨",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer(
        f"🔍 <b>Ищу треки по запросу:</b> <i>«{html.escape(query)}»</i>...\n<code>[███░░░░░░░░░] 25%</code>",
        parse_mode="HTML",
    )

    try:
        warp_proxy_url = None
        if warp_proxy and hasattr(warp_proxy, "warp") and warp_proxy.warp.enabled and warp_proxy.warp.supported:
            try:
                if await warp_proxy.warp.ensure_ready(timeout=20.0):
                    warp_proxy_url = warp_proxy.warp.proxy_url
            except Exception:
                pass

        tracks = await music_service.search_tracks(query, limit=5, proxy_url=warp_proxy_url)
        if not tracks and warp_proxy_url:
            tracks = await music_service.search_tracks(query, limit=5, proxy_url=None)

        if not tracks:
            await a.safe_edit_status(
                status_msg,
                f"❌ <b>По запросу «{html.escape(query)}» ничего не найдено.</b>\n"
                f"Попробуй изменить запрос или указать имя исполнителя.",
            )
            return

        sid = music_service.save_search_session(query, tracks)

        lines = [f"🎵 <b>Результаты поиска:</b> <i>«{html.escape(query)}»</i>\n"]
        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        buttons_row = []

        for idx, tr in enumerate(tracks):
            icon = num_emojis[idx] if idx < len(num_emojis) else f"[{idx+1}]"
            t_title = html.escape(tr.get("title", "Трек"))
            t_artist = html.escape(tr.get("artist", "Исполнитель"))
            t_dur = tr.get("duration_str", "")
            lines.append(f"{icon} <b>{t_title}</b> — {t_artist} <code>({t_dur})</code>")
            buttons_row.append(
                InlineKeyboardButton(text=icon, callback_data=f"dl_m:{sid}:{idx}")
            )

        lines.append("\n<i>Нажми на номер трека ниже, чтобы скачать 👇</i>")
        text = "\n".join(lines)

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                buttons_row,
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_m:{sid}")],
            ]
        )

        await a.safe_edit_status(status_msg, text, reply_markup=keyboard)

    except Exception as e:
        logging.error(f"Music search handler error: {e}")
        await a.safe_edit_status(status_msg, "❌ <b>Произошла ошибка при поиске музыки.</b> Попробуйте позже.")


@router.callback_query(F.data.startswith("dl_m:"))
async def handle_music_download_callback(callback: CallbackQuery):
    a = _app()
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    sid = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    track = music_service.get_search_track(sid, idx)
    if not track:
        await callback.answer("⚠️ Сессия поиска устарела. Пожалуйста, выполните поиск заново.", show_alert=True)
        return

    t_title = track.get("title", "Трек")
    t_artist = track.get("artist", "Исполнитель")
    await callback.answer(f"⏳ Скачиваю: {t_title[:30]}...")

    status_msg = callback.message

    await a.safe_edit_status(
        status_msg,
        f"🎵 <b>Скачиваю трек:</b> «{html.escape(t_title)} — {html.escape(t_artist)}»...\n"
        f"<code>[██░░░░░░░░░░] 15%</code>\n"
        f"🔍 Подключаюсь к аудиопотоку...",
    )

    c_info = {"label": f"трек «{t_title}»", "icon": "🎵"}
    progress_cb = a.create_progress_reporter(status_msg, c_info, throttle_seconds=1.0)

    uid = str(uuid.uuid4())[:8]
    output_prefix = os.path.join(DOWNLOAD_DIR, f"music_{callback.from_user.id}_{uid}")

    try:
        warp_proxy_url = None
        if warp_proxy and hasattr(warp_proxy, "warp") and warp_proxy.warp.enabled and warp_proxy.warp.supported:
            try:
                if await warp_proxy.warp.ensure_ready(timeout=20.0):
                    warp_proxy_url = warp_proxy.warp.proxy_url
            except Exception:
                pass

        res = await music_service.download_track(
            track,
            output_prefix,
            progress_callback=progress_cb,
            proxy_url=warp_proxy_url,
        )

        if not res or not res.get("file_path") or not os.path.exists(res["file_path"]):
            raise Exception("audio_file_missing")

        await a.safe_edit_status(
            status_msg,
            "🎵 <b>Отправляю в Telegram...</b>\n<code>[████████████] 100%</code>",
        )

        thumb_path = res.get("thumbnail_path")
        thumb_file = FSInputFile(thumb_path) if (thumb_path and os.path.exists(thumb_path)) else None

        caption = (
            f"🎧 <b>{html.escape(res.get('title', 'Трек'))}</b> — {html.escape(res.get('performer', 'Исполнитель'))}\n"
            f"✨ <i>Скачано через @duo_play_date_bot</i>"
        )

        target_chat = callback.message.chat.id if callback.message else callback.from_user.id
        await a.bot.send_audio(
            chat_id=target_chat,
            audio=FSInputFile(res["file_path"]),
            performer=res.get("performer"),
            title=res.get("title"),
            duration=res.get("duration", 0),
            thumbnail=thumb_file,
            caption=caption,
            parse_mode="HTML",
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as dl_err:
        logging.error(f"Error downloading selected track: {dl_err}")
        err_msg = a.format_friendly_error(dl_err, platform="music")
        await a.safe_edit_status(
            status_msg,
            f"❌ <b>Не удалось скачать аудиозапись:</b> {err_msg}",
        )
    finally:
        try:
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(f"music_{callback.from_user.id}_{uid}"):
                    p = os.path.join(DOWNLOAD_DIR, f)
                    if os.path.exists(p):
                        os.remove(p)
        except Exception:
            pass


@router.callback_query(F.data.startswith("cancel_m:"))
async def handle_cancel_music_callback(callback: CallbackQuery):
    await callback.answer("Поиск отменён")
    try:
        await callback.message.delete()
    except Exception:
        pass
