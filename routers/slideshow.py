"""
Модуль интерактивных каруселей / слайдшоу (TikTok и Instagram).
Поддерживает Fast-Track старт (мгновенная отправка 1-го слайда и фоновая докачка остальных),
предзагрузку file_id в Telegram и скачивание аудиодорожки поста.
"""

import os
import html
import asyncio
import logging
import tempfile
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    FSInputFile,
)

from . import app as _app

logger = logging.getLogger(__name__)
router = Router(name="slideshow")


def _is_video_slide(target) -> bool:
    return str(target or "").split("?")[0].lower().endswith((".mp4", ".mov", ".m4v"))


def _extract_msg_fid(msg) -> Optional[str]:
    if not msg:
        return None
    if getattr(msg, "video", None):
        return msg.video.file_id
    if getattr(msg, "photo", None):
        return msg.photo[-1].file_id
    return None


async def expire_slideshow_session(sid: str, delay_seconds: int = 3600):
    """Удаляет временные файлы со слайдами после завершения активной сессии."""
    a = _app()
    await asyncio.sleep(delay_seconds)
    data = a._slideshow_cache.pop(sid, None)
    if data and data.get("photos"):
        for p in data["photos"]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


async def preupload_slideshow_file_ids(sid: str):
    """
    Фоновая предзагрузка слайдов в Telegram (включая ожидание Fast-Track фоновой докачки).
    Ускоряет перелистывание и исключает 403 при смене слайдов.
    """
    a = _app()
    data = a._slideshow_cache.get(sid)
    if not data or not data.get("photos"):
        return

    bg_task = data.get("_bg_task")
    photos = data["photos"]
    fids = data.get("file_ids") or []
    if len(fids) < len(photos):
        fids.extend([None] * (len(photos) - len(fids)))
        data["file_ids"] = fids

    for idx, p_path in enumerate(photos):
        if data["file_ids"][idx] is not None:
            continue
        # Для Fast-Track: ждём до 8 сек появления очередного файла на диске от фоновой задачи
        if p_path and not os.path.exists(p_path) and bg_task and not bg_task.done():
            for _ in range(20):
                if os.path.exists(p_path) or bg_task.done():
                    break
                await asyncio.sleep(0.4)

        if data["file_ids"][idx] is None and p_path and os.path.exists(p_path):
            try:
                is_vid = _is_video_slide(p_path)
                fid = await a.upload_media_to_telegram(p_path, is_photo=not is_vid)
                if fid and sid in a._slideshow_cache:
                    a._slideshow_cache[sid]["file_ids"][idx] = fid
                    a.save_slideshow_session_db(sid, a._slideshow_cache[sid])
            except Exception as up_err:
                logging.debug(f"Preupload slide #{idx} failed: {up_err}")


def make_slide_caption(data: dict, current_idx: int, total: int) -> str:
    """Формирует компактную подпись к слайду со скрытым описанием <tg-spoiler> и треком."""
    a = _app()
    plat_raw = (data.get("platform") or "TikTok").capitalize()
    author_raw = (data.get("author") or "").strip()[:50]
    author_esc = html.escape(author_raw)

    lines = []
    desc_line = a.format_description_line(data.get("title") or "", is_photo=True)
    if desc_line:
        lines.append(desc_line)

    music_line = a.format_music_line(
        music_info=data.get("music_info"),
        music=data.get("music"),
        post_title=data.get("title"),
    )
    if music_line:
        lines.append(music_line)

    meta = f"📱 <b>Источник:</b> {plat_raw}"
    if author_esc and author_esc.lower() not in ("tiktok", "instagram", "media", "видео", "creator", "unknown"):
        meta += f" | {author_esc}"
    lines.append(meta)

    if total > 1:
        lines.append(f"🖼 <b>Слайд {current_idx + 1} из {total}</b> <i>(листай стрелками ниже 👇)</i>")

    lines.append("✨ <i>Скачано через @duo_play_date_bot</i>")
    return "\n".join(lines)


def build_slideshow_keyboard(
    sid: str,
    current_idx: int,
    total: int,
    data: dict,
    is_inline: bool = False,
    is_collapsed: bool = False,
) -> InlineKeyboardMarkup:
    """Создает клавиатуру управления слайдшоу."""
    if is_collapsed:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🔼 Листать слайды ({current_idx + 1}/{total})",
                        callback_data=f"slide_expand:{sid}:{current_idx}",
                    )
                ]
            ]
        )

    btn_prev = InlineKeyboardButton(
        text="⬅️" if current_idx > 0 else "🔘",
        callback_data=f"slide:{sid}:{current_idx - 1}" if current_idx > 0 else "slide_noop",
    )
    btn_counter = InlineKeyboardButton(
        text=f"{current_idx + 1}/{total}",
        callback_data=f"slide_info:{current_idx + 1}:{total}",
    )
    btn_next = InlineKeyboardButton(
        text="➡️" if current_idx < total - 1 else "🔘",
        callback_data=f"slide:{sid}:{current_idx + 1}" if current_idx < total - 1 else "slide_noop",
    )
    rows = [[btn_prev, btn_counter, btn_next]]

    m_info = data.get("music_info") or {}
    m_id = m_info.get("id") if isinstance(m_info, dict) else None
    music_u = data.get("music") or (m_info.get("play") if isinstance(m_info, dict) else None)

    if is_inline:
        row2 = []
        if m_id:
            row2.append(InlineKeyboardButton(text="🎵 Звук", url=f"https://www.tiktok.com/music/-{m_id}"))
        elif music_u and str(music_u).startswith("http"):
            row2.append(InlineKeyboardButton(text="🎵 Звук", url=str(music_u)))
        if row2:
            rows.append(row2)
    else:
        row_pm = [
            InlineKeyboardButton(
                text="📥 Скачать все слайды альбомом",
                callback_data=f"album_all:{sid}",
            )
        ]
        if music_u:
            row_pm.append(
                InlineKeyboardButton(
                    text="🎧 Скачать MP3",
                    callback_data=f"music:{sid}",
                )
            )
        rows.append(row_pm)

    rows.append([
        InlineKeyboardButton(
            text="🔽 Скрыть кнопки",
            callback_data=f"slide_collapse:{sid}:{current_idx}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("slide:"))
async def handle_slide_switch_callback(callback: CallbackQuery):
    a = _app()
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

    data = a.get_slideshow_session_db(sid)
    if not data:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Загрузить слайды заново", callback_data=f"refresh_slide:{sid}")
        ]])
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
            elif callback.inline_message_id:
                await a.bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id, reply_markup=kb)
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
    if len(file_ids) < total:
        file_ids.extend([None] * (total - len(file_ids)))
        data["file_ids"] = file_ids
    photo_urls = data.get("photo_urls") or []
    photos = data.get("photos") or []

    cached_fid = file_ids[new_idx] if new_idx < len(file_ids) else None
    photo_u = photo_urls[new_idx] if new_idx < len(photo_urls) else None
    file_path = photos[new_idx] if new_idx < len(photos) else None

    # Если слайд ещё качается в Fast-Track фоне — кратко дожидаемся его на диске
    bg_task = data.get("_bg_task")
    if not cached_fid and file_path and not os.path.exists(file_path) and bg_task and not bg_task.done():
        for _ in range(12):
            if os.path.exists(file_path) or bg_task.done():
                break
            await asyncio.sleep(0.3)

    local_exists = bool(file_path and os.path.exists(file_path))

    # Приоритет источника:
    # 1. Telegram file_id -> 2. Локальный файл на диске -> 3. CDN URL (резерв)
    if callback.inline_message_id:
        if not cached_fid and local_exists:
            is_vid_local = _is_video_slide(file_path)
            uid = callback.from_user.id if callback.from_user else None
            cached_fid = await a.upload_media_to_telegram(
                file_path,
                is_photo=not is_vid_local,
                target_user_id=uid,
                title=data.get("title", ""),
                platform=data.get("platform", ""),
            )
            if cached_fid:
                data["file_ids"][new_idx] = cached_fid
                a.save_slideshow_session_db(sid, data)

        media_target = cached_fid or photo_u
        if not media_target:
            await callback.answer("❌ Слайд временно недоступен.", show_alert=True)
            return

        is_vid = _is_video_slide(file_path) if local_exists else _is_video_slide(media_target)
        media_cls = InputMediaVideo if is_vid else InputMediaPhoto
        try:
            await a.bot.edit_message_media(
                inline_message_id=callback.inline_message_id,
                media=media_cls(media=media_target, caption=caption, parse_mode="HTML"),
                reply_markup=kb,
            )
        except Exception as in_err:
            logging.debug(f"Inline slide edit error: {in_err}")
        await callback.answer()
        return

    media_target = cached_fid or (FSInputFile(file_path) if local_exists else photo_u)
    if not media_target:
        await callback.answer("❌ Слайд временно недоступен.", show_alert=True)
        return

    is_vid = _is_video_slide(file_path) if local_exists else _is_video_slide(media_target)
    media_cls = InputMediaVideo if is_vid else InputMediaPhoto
    media_obj = media_cls(media=media_target, caption=caption, parse_mode="HTML")

    try:
        if callback.message:
            res_msg = await callback.message.edit_media(media=media_obj, reply_markup=kb)
            new_fid = _extract_msg_fid(res_msg)
            if new_fid:
                data["file_ids"][new_idx] = new_fid
                a.save_slideshow_session_db(sid, data)
        await callback.answer()
    except Exception as e:
        logging.warning(f"Slide edit media error: {e}")
        if local_exists and callback.message:
            try:
                if is_vid:
                    tmp_sent = await a.bot.send_video(chat_id=callback.message.chat.id, video=FSInputFile(file_path), disable_notification=True)
                else:
                    tmp_sent = await a.bot.send_photo(chat_id=callback.message.chat.id, photo=FSInputFile(file_path), disable_notification=True)
                new_fid = _extract_msg_fid(tmp_sent)
                if tmp_sent:
                    try:
                        await tmp_sent.delete()
                    except Exception:
                        pass
                if new_fid:
                    data["file_ids"][new_idx] = new_fid
                    a.save_slideshow_session_db(sid, data)
                    await callback.message.edit_media(
                        media=media_cls(media=new_fid, caption=caption, parse_mode="HTML"),
                        reply_markup=kb,
                    )
            except Exception as retry_err:
                logging.debug(f"Slide edit media retry error: {retry_err}")
        await callback.answer()


@router.callback_query(F.data.startswith("refresh_slide:"))
async def handle_refresh_slide_callback(callback: CallbackQuery):
    a = _app()
    sid = callback.data.split(":")[1] if ":" in callback.data else ""
    await callback.answer("🔄 Проверяю сессию...", show_alert=False)
    data = a.get_slideshow_session_db(sid)
    if data:
        total = data.get("total", 1)
        is_in = bool(callback.inline_message_id)
        kb = build_slideshow_keyboard(sid, 0, total, data, is_inline=is_in)
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=kb)
            elif callback.inline_message_id:
                await a.bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id, reply_markup=kb)
        except Exception:
            pass
    else:
        await callback.answer("ℹ️ Отправьте ссылку заново в чат, чтобы создать новую активную сессию.", show_alert=True)


@router.callback_query(F.data.startswith("slide_collapse:"))
async def handle_slide_collapse_callback(callback: CallbackQuery):
    a = _app()
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    sid = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        idx = 0
    data = a.get_slideshow_session_db(sid)
    if not data:
        await callback.answer("Сессия слайдшоу устарела.", show_alert=False)
        return
    total = data.get("total", 1)
    is_in = bool(callback.inline_message_id)
    kb = build_slideshow_keyboard(sid, idx, total, data, is_inline=is_in, is_collapsed=True)
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=kb)
        elif callback.inline_message_id:
            await a.bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id, reply_markup=kb)
    except Exception:
        pass
    await callback.answer("Кнопки скрыты. Нажми «🔼 Листать слайды», чтобы вернуть.")


@router.callback_query(F.data.startswith("slide_expand:"))
async def handle_slide_expand_callback(callback: CallbackQuery):
    a = _app()
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    sid = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        idx = 0
    data = a.get_slideshow_session_db(sid)
    if not data:
        await callback.answer("Сессия слайдшоу устарела.", show_alert=False)
        return
    total = data.get("total", 1)
    is_in = bool(callback.inline_message_id)
    kb = build_slideshow_keyboard(sid, idx, total, data, is_inline=is_in, is_collapsed=False)
    try:
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=kb)
        elif callback.inline_message_id:
            await a.bot.edit_message_reply_markup(inline_message_id=callback.inline_message_id, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("stat:"))
async def handle_slide_stat_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    stat_type = parts[1]
    sid = parts[2]
    data = _app().get_slideshow_session_db(sid)
    if not data:
        await callback.answer("Статистика недоступна для старой сессии.", show_alert=False)
        return
    d_cnt = int(data.get("digg_count", 0) or 0)
    c_cnt = int(data.get("comment_count", 0) or 0)
    if stat_type == "likes":
        await callback.answer(f"❤️ Нравится: {d_cnt:,}".replace(",", " "), show_alert=False)
    elif stat_type == "comments":
        await callback.answer(f"💬 Комментарии: {c_cnt:,}".replace(",", " "), show_alert=False)
    else:
        await callback.answer()


@router.callback_query(F.data.startswith("music:"))
async def handle_slide_music_callback(callback: CallbackQuery):
    a = _app()
    parts = callback.data.split(":")
    sid = parts[1] if len(parts) > 1 else ""
    data = a.get_slideshow_session_db(sid)
    if not data or not data.get("music"):
        await callback.answer("🎵 В этом посте нет отдельной аудиодорожки.", show_alert=True)
        return

    music_url = data["music"]
    music_info = data.get("music_info") or {}
    m_title = (music_info.get("title") or data.get("title") or "TikTok Audio")[:80]
    m_author = (music_info.get("author") or data.get("author") or "TikTok")[:50]
    m_dur = int(music_info.get("duration", 0))

    await callback.answer("🎵 Отправляю аудиодорожку...")
    tmp_audio = os.path.join(tempfile.gettempdir(), f"tt_music_{sid}.mp3")
    try:
        import tiktok_service
        if not os.path.exists(tmp_audio):
            await tiktok_service.download_file_stream(music_url, tmp_audio)
        audio_file = FSInputFile(tmp_audio)
        target_chat = callback.message.chat.id if callback.message else callback.from_user.id
        await a.bot.send_audio(
            chat_id=target_chat,
            audio=audio_file,
            title=m_title,
            performer=m_author,
            duration=m_dur,
            caption=f"🎵 <b>{html.escape(m_title)}</b>\n👤 {html.escape(m_author)}\n✨ <i>@duo_play_date_bot</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        logging.warning(f"Failed to send tiktok audio: {e}")
        target_chat = callback.message.chat.id if callback.message else callback.from_user.id
        try:
            await a.bot.send_message(chat_id=target_chat, text=f"⚠️ Не удалось отправить аудиодорожку: {e}")
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp_audio):
                os.remove(tmp_audio)
        except Exception:
            pass


@router.callback_query(F.data.startswith("album_all:"))
async def handle_slide_album_all_callback(callback: CallbackQuery):
    a = _app()
    parts = callback.data.split(":")
    sid = parts[1] if len(parts) > 1 else ""
    data = a.get_slideshow_session_db(sid)
    if not data or not (data.get("photos") or data.get("photo_urls")):
        await callback.answer("⚠️ Сессия слайдшоу устарела.", show_alert=True)
        return

    await callback.answer("📥 Отправляю все слайды альбомом...")

    bg_task = data.get("_bg_task")
    if bg_task and not bg_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(bg_task), timeout=12.0)
        except Exception:
            pass

    photos = data.get("photos") or []
    photo_urls = data.get("photo_urls") or []
    file_ids = data.get("file_ids") or []
    total_cnt = max(len(photos), len(photo_urls))

    plat_raw = (data.get("platform") or "Медиа").capitalize()
    default_title = f"{plat_raw} Альбом"
    title_esc = html.escape((data.get("title") or default_title)[:200])
    author_raw = (data.get("author") or "")[:50]
    author_esc = html.escape(author_raw)
    album_caption = f"📸 <b>{title_esc}</b>\n\n📱 <b>Источник:</b> {plat_raw}"
    if author_esc and author_esc.lower() not in ("tiktok", "instagram", "media", "видео"):
        album_caption += f" | {author_esc}"
    album_caption += f"\n🖼 <b>Всего слайдов:</b> {total_cnt}\n✨ <i>Скачано через @duo_play_date_bot</i>"

    items = []
    for idx in range(total_cnt):
        fid = file_ids[idx] if idx < len(file_ids) else None
        p_path = photos[idx] if idx < len(photos) else None
        p_url = photo_urls[idx] if idx < len(photo_urls) else None
        if fid:
            media_src = fid
        elif p_path and os.path.exists(p_path):
            media_src = FSInputFile(p_path)
        elif p_url:
            media_src = p_url
        else:
            continue
        is_vid = _is_video_slide(p_path or fid or p_url)
        items.append((media_src, is_vid))

    for chunk_start in range(0, len(items), 10):
        chunk = items[chunk_start:chunk_start + 10]
        media_group = []
        for i, (m_src, is_vid) in enumerate(chunk):
            media_cls = InputMediaVideo if is_vid else InputMediaPhoto
            if chunk_start == 0 and i == 0:
                media_group.append(media_cls(media=m_src, caption=album_caption, parse_mode="HTML"))
            else:
                media_group.append(media_cls(media=m_src))
        if media_group:
            try:
                target_chat = callback.message.chat.id if callback.message else callback.from_user.id
                await a.bot.send_media_group(chat_id=target_chat, media=media_group)
            except Exception as e:
                logging.warning(f"Failed to send media group chunk: {e}")


@router.callback_query(F.data.startswith("slide_info:"))
async def handle_slide_info_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) == 3:
        await callback.answer(f"Слайд {parts[1]} из {parts[2]}", show_alert=False)
    else:
        await callback.answer()


@router.callback_query(F.data == "slide_noop")
async def handle_slide_noop_callback(callback: CallbackQuery):
    await callback.answer()
