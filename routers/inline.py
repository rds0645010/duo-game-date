"""
Инлайн-режим (@duo_play_date_bot <ссылка>):
мгновенная отдача из кэша или надёжная двухфазная подмена через edit_message_media,
включая Fast-Track старт фото/видео слайдшоу.
"""

import os
import html
import time
import uuid
import asyncio
import logging
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAudio,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChosenInlineResult,
    CallbackQuery,
)

from downloader import (
    detect_platform,
    detect_content_info,
    extract_url,
    download_video,
    is_valid_mp4_file,
)
from . import app as _app
from .slideshow import make_slide_caption, build_slideshow_keyboard, preupload_slideshow_file_ids

logger = logging.getLogger(__name__)
router = Router(name="inline")

_active_inline_jobs = set()


def make_inline_caption(
    title: str = "",
    platform: str = "",
    author: str = "",
    music_info: dict = None,
    music: str = None,
    is_photo: bool = False,
    is_audio: bool = False,
    caption_enabled: bool = True,
) -> Optional[str]:
    """
    Генерирует аккуратную подпись для отправки медиа в инлайн-режиме:
    - Скрытое описание <tg-spoiler> (всегда свёрнуто, без лишних хэштегов)
    - Играющий музыкальный трек (если есть и не дублирует описание)
    - Источник и автор
    """
    if not caption_enabled:
        return None
    a = _app()
    plat_name = (platform or "медиа").capitalize()
    author_raw = (author or "").strip()[:50]
    author_esc = html.escape(author_raw)

    lines = []
    desc_line = a.format_description_line(title, is_photo=is_photo, is_audio=is_audio)
    if desc_line:
        lines.append(desc_line)

    music_line = a.format_music_line(music_info=music_info, music=music, post_title=title)
    if music_line:
        lines.append(music_line)

    meta = f"📱 <b>Источник:</b> {plat_name}"
    if author_esc and author_esc.lower() not in ("tiktok", "instagram", "media", "видео", "creator", "unknown"):
        meta += f" | {author_esc}"
    lines.append(meta)

    lines.append("✨ <i>Скачано через @duo_play_date_bot</i>")
    return "\n\n".join(lines)


@router.inline_query()
async def inline_query_handler(inline_query: InlineQuery):
    a = _app()
    query = inline_query.query.strip()
    user_id = inline_query.from_user.id
    caption_enabled = a.get_user_inline_caption(user_id)

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
                    parse_mode="HTML",
                ),
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
                ),
            )
        ]
        await inline_query.answer(results=results, cache_time=10, is_personal=True)
        return

    logging.info(f"🔎 [INLINE] user={user_id} platform={platform} url={clean_url}")

    c_info = detect_content_info(clean_url)
    p_noun = c_info.get("label", "медиа")
    p_icon = c_info.get("icon", "🎬")
    plat_source = c_info.get("platform", "").capitalize() or "Медиа"

    if platform == "instagram":
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/instagram.png"
    elif platform == "tiktok":
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/tiktok.png"
    else:
        plat_icon = "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/youtube.png"

    inline_title = f"💌 Поделиться {p_noun}"

    # 1. Кэш
    cached = a.get_cached_media(clean_url)
    if a.drop_poisoned_media_cache(clean_url, cached):
        cached = None
    if cached and ("/photo/" in clean_url or "/p/" in clean_url):
        logging.info(f"Bypassing cache for potential album: {clean_url}")
        cached = None
    if cached and cached.get("file_id"):
        m_type = cached.get("media_type", "video")
        logging.info(f"⚡ [INLINE] Cache hit ({m_type}) for {clean_url}")
        cap = make_inline_caption(
            title=cached.get("title") or "",
            platform=cached.get("platform") or plat_source,
            author=cached.get("author") or "",
            music_info=cached.get("music_info"),
            music=cached.get("music"),
            is_photo=(m_type == "photo"),
            is_audio=(m_type == "audio"),
            caption_enabled=caption_enabled,
        )
        if m_type == "photo":
            single_result = InlineQueryResultCachedPhoto(
                id=f"c_p_{abs(hash(cached['file_id']))}",
                photo_file_id=cached["file_id"],
                title=inline_title,
                caption=cap,
                parse_mode="HTML" if cap else None,
            )
        elif m_type == "audio":
            single_result = InlineQueryResultCachedAudio(
                id=f"c_a_{abs(hash(cached['file_id']))}",
                audio_file_id=cached["file_id"],
                caption=cap,
                parse_mode="HTML" if cap else None,
            )
        else:
            single_result = InlineQueryResultCachedVideo(
                id=f"c_v_{abs(hash(cached['file_id']))}",
                video_file_id=cached["file_id"],
                title=inline_title,
                caption=cap,
                parse_mode="HTML" if cap else None,
            )
        await inline_query.answer(results=[single_result], cache_time=300, is_personal=True)
        return

    # 2. Надёжная двухфазная подмена для всех некэшированных ссылок (включая TikTok)
    token = a.save_dl_token(clean_url)
    fresh_id = f"art_v4_{token}_{uuid.uuid4().hex[:6]}"
    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"⏳ Загрузка {p_noun}...", callback_data=f"get_vid:{token}")]]
    )
    article_result = InlineQueryResultArticle(
        id=fresh_id,
        title=inline_title,
        description=f"Нажми, чтобы {p_noun} улетело прямо в чат ✨",
        thumbnail_url=plat_icon,
        input_message_content=InputTextMessageContent(
            message_text=(
                f"{p_icon} <b>Загружаю {p_noun}...</b>\n"
                f"<code>[██░░░░░░░░░░] 15%</code>\n"
                f"<i>Скоро появится в этом сообщении ✨</i>"
            ),
            parse_mode="HTML",
        ),
        reply_markup=loading_kb,
    )
    try:
        await inline_query.answer(results=[article_result], cache_time=0, is_personal=True)
    except Exception as ans_err:
        logging.error(f"Error answering inline query: {ans_err}")


async def execute_inline_delivery(inline_message_id: str, token: str, user_id: int):
    """Единая функция скачивания и подмены сообщения в инлайне с живым прогресс-баром."""
    a = _app()
    job_key = f"{inline_message_id}_{token}"
    if job_key in _active_inline_jobs:
        return
    _active_inline_jobs.add(job_key)

    try:
        url = a.get_dl_token_url(token)
        if not url:
            try:
                await a.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text="⚠️ <b>Ссылка устарела.</b> Пожалуйста, вызовите бота заново через инлайн.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return

        c_info = detect_content_info(url)
        p_noun = c_info.get("label", "медиа")
        p_icon = c_info.get("icon", "⚡")
        plat_source = c_info.get("platform", "").capitalize() or "Медиа"

        loading_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"⏳ Загрузка {p_noun}...", callback_data=f"get_vid:{token}")]]
        )

        cached = a.get_cached_media(url)
        if a.drop_poisoned_media_cache(url, cached):
            cached = None
        file_id = cached.get("file_id") if cached else None
        is_photo = (cached.get("media_type") == "photo") if cached else False
        is_audio = (cached.get("media_type") == "audio") if cached else False
        stream_fallback_url = None
        res = None

        if not file_id:
            try:
                await a.safe_edit_status(
                    inline_message_id=inline_message_id,
                    text=(
                        f"{p_icon} <b>Загружаю {p_noun}...</b>\n"
                        f"<code>[███░░░░░░░░░] 25%</code>\n"
                        f"🔍 Подключаюсь и начинаю скачивание..."
                    ),
                    reply_markup=loading_kb,
                )

                progress_cb = a.create_progress_reporter(
                    status_msg=None,
                    content_info_or_name=c_info,
                    inline_message_id=inline_message_id,
                    reply_markup=loading_kb,
                    throttle_seconds=1.2,
                )
                res = await download_video(url, user_id, progress_callback=progress_cb)

                # Обработка каруселей / слайдшоу (включая Fast-Track)
                if res and isinstance(res, dict) and res.get("is_album") and (res.get("photos") or res.get("photo_urls")):
                    photos = res.get("photos") or []
                    photo_urls = res.get("photo_urls") or []
                    total_slides = max(len(photos), len(photo_urls))

                    await a.safe_edit_status(
                        inline_message_id=inline_message_id,
                        text=(
                            f"📸 <b>Подготавливаю слайдшоу ({total_slides} слайдов)...</b>\n"
                            f"<code>[████████████] 100%</code>\n"
                            f"✨ Отправляю в чат..."
                        ),
                        reply_markup=loading_kb,
                    )

                    sid = uuid.uuid4().hex[:10]
                    plat_name = res.get("platform", "tiktok")
                    default_ttl = f"{plat_name.capitalize()} Слайдшоу"
                    a._slideshow_cache[sid] = {
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
                        "created_at": time.time(),
                        "_bg_task": res.get("_bg_task"),
                    }
                    a.save_slideshow_session_db(sid, a._slideshow_cache[sid])

                    first_file = photos[0] if (photos and os.path.exists(photos[0])) else None
                    first_u = photo_urls[0] if photo_urls else None
                    is_vid_first = bool(first_file and first_file.lower().endswith((".mp4", ".mov", ".m4v")))
                    first_fid = await a.upload_media_to_telegram(
                        first_file, is_photo=not is_vid_first, target_user_id=user_id, force_ephemeral=True
                    ) if first_file else None
                    if first_fid:
                        a._slideshow_cache[sid]["file_ids"][0] = first_fid
                        a.save_slideshow_session_db(sid, a._slideshow_cache[sid])

                    media_target = first_fid or first_u
                    if not media_target:
                        err_kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🤖 Запустить бота (@duo_play_date_bot)", url="https://t.me/duo_play_date_bot?start=1")],
                            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"retry_dl:{token}")],
                        ])
                        await a.safe_edit_status(
                            inline_message_id=inline_message_id,
                            text=(
                                "⚠️ <b>Не удалось загрузить слайд в Telegram.</b>\n\n"
                                "Нажмите <b>«Запустить бота»</b> (/start) один раз, затем нажмите «Попробовать снова» 👇"
                            ),
                            reply_markup=err_kb,
                        )
                        return

                    caption = make_slide_caption(a._slideshow_cache[sid], 0, total_slides)
                    kb = build_slideshow_keyboard(sid, 0, total_slides, a._slideshow_cache[sid], is_inline=True)
                    is_vid = is_vid_first or str(media_target).lower().endswith((".mp4", ".mov", ".m4v"))
                    media_cls = InputMediaVideo if is_vid else InputMediaPhoto
                    try:
                        await a.bot.edit_message_media(
                            inline_message_id=inline_message_id,
                            media=media_cls(media=media_target, caption=caption, parse_mode="HTML"),
                            reply_markup=kb,
                        )
                    except Exception as emm_err:
                        if hasattr(emm_err, "retry_after"):
                            await asyncio.sleep(getattr(emm_err, "retry_after") + 0.2)
                            await a.bot.edit_message_media(
                                inline_message_id=inline_message_id,
                                media=media_cls(media=media_target, caption=caption, parse_mode="HTML"),
                                reply_markup=kb,
                            )
                        else:
                            raise emm_err
                    a._stream_files.pop(token, None)
                    if total_slides > 1:
                        a.track_background_task(preupload_slideshow_file_ids(sid))
                    return

                if res and isinstance(res, dict) and res.get("file_path"):
                    await a.safe_edit_status(
                        inline_message_id=inline_message_id,
                        text=(
                            f"📤 <b>Отправляю {p_noun} в чат...</b>\n"
                            f"<code>[████████████] 100%</code>"
                        ),
                        reply_markup=loading_kb,
                    )

                    is_photo = bool(res.get("is_photo"))
                    is_audio = bool(res.get("is_audio"))
                    if not is_audio and is_valid_mp4_file(res["file_path"], allow_audio=False):
                        is_photo = False
                    if is_photo and a.is_instagram_reel_url(url):
                        raise Exception("instagram_video_unavailable")

                    file_id = await a.upload_media_to_telegram(
                        res["file_path"],
                        is_photo=is_photo,
                        is_audio=is_audio,
                        target_user_id=user_id,
                        title=res.get("title", ""),
                        platform=res.get("platform", ""),
                        performer=res.get("performer", ""),
                        duration=int(res.get("duration", 0)),
                    )
                    if file_id:
                        m_type = "audio" if is_audio else ("photo" if is_photo else "video")
                        a.save_cached_media(
                            url,
                            file_id,
                            m_type,
                            res.get("title", ""),
                            res.get("platform", ""),
                            res.get("author", "") or res.get("performer", ""),
                            music=res.get("music"),
                            music_info=res.get("music_info"),
                        )
                        try:
                            if os.path.exists(res["file_path"]):
                                os.remove(res["file_path"])
                        except Exception:
                            pass
                        a._stream_files.pop(token, None)
                    else:
                        f_path = res["file_path"]
                        f_sz = os.path.getsize(f_path) if os.path.exists(f_path) else 0
                        max_stream_sz = 5 * 1024 * 1024 if is_photo else 20 * 1024 * 1024
                        if (not is_audio) and a.RENDER_EXTERNAL_URL and 0 < f_sz <= max_stream_sz:
                            a._stream_files[token] = f_path
                            a.update_dl_token_file(token, f_path)
                            a.track_background_task(a._expire_stream_file(token, f_path, delay=300))
                            ext = "jpg" if is_photo else "mp4"
                            stream_fallback_url = f"{a.RENDER_EXTERNAL_URL}/stream/{token}.{ext}"
                            logging.info(f"Using stream fallback URL for inline delivery: {stream_fallback_url}")
                        else:
                            try:
                                if os.path.exists(f_path):
                                    os.remove(f_path)
                            except Exception:
                                pass
                            bot_kb = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="🤖 Запустить бота (@duo_play_date_bot)", url="https://t.me/duo_play_date_bot?start=1")],
                                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"retry_dl:{token}")],
                            ])
                            await a.safe_edit_status(
                                inline_message_id=inline_message_id,
                                text=(
                                    "⚠️ <b>Для отправки видео в этот чат нужно один раз активировать бота.</b>\n\n"
                                    "Нажмите <b>«Запустить бота»</b> (/start) и вернитесь в чат — затем нажмите «Попробовать снова» 👇"
                                ),
                                reply_markup=bot_kb,
                            )
                            return
                else:
                    raise Exception("download_failed")
            except Exception as e:
                logging.error(f"Inline download error for {url}: {e}")
                target_platform = detect_platform(url)
                err_text = a.format_friendly_error(e, platform=target_platform)
                retry_kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"retry_dl:{token}")]]
                )
                try:
                    await a.bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=f"{err_text}\n\n<i>Нажмите кнопку ниже для повторной попытки 👇</i>",
                        parse_mode="HTML",
                        reply_markup=retry_kb,
                    )
                except Exception:
                    pass
                return

        media_src = file_id or stream_fallback_url
        if media_src:
            caption_enabled = a.get_user_inline_caption(user_id)
            title = ""
            author = ""
            plat = ""
            m_info = None
            music_val = None
            if res and isinstance(res, dict):
                title = res.get("title") or ""
                author = res.get("author") or res.get("performer") or ""
                plat = res.get("platform") or ""
                m_info = res.get("music_info")
                music_val = res.get("music")
            elif cached and isinstance(cached, dict):
                title = cached.get("title") or ""
                author = cached.get("author") or ""
                plat = cached.get("platform") or ""
                m_info = cached.get("music_info")
                music_val = cached.get("music")

            caption = make_inline_caption(
                title=title,
                platform=plat or plat_source,
                author=author,
                music_info=m_info,
                music=music_val,
                is_photo=is_photo,
                is_audio=is_audio,
                caption_enabled=caption_enabled,
            )

            try:
                if is_audio:
                    media_obj = InputMediaAudio(
                        media=media_src,
                        caption=caption,
                        parse_mode="HTML" if caption else None,
                        title=(res.get("title") if res else (cached.get("title") if cached else "")),
                        performer=(res.get("performer") if res else (cached.get("author") if cached else "")),
                        duration=int(res.get("duration", 0)) if res else 0,
                    )
                elif is_photo:
                    media_obj = InputMediaPhoto(media=media_src, caption=caption, parse_mode="HTML" if caption else None)
                else:
                    media_obj = InputMediaVideo(
                        media=media_src,
                        caption=caption,
                        parse_mode="HTML" if caption else None,
                        supports_streaming=True,
                    )
                try:
                    await a.bot.edit_message_media(
                        inline_message_id=inline_message_id,
                        media=media_obj,
                        reply_markup=None,
                    )
                except Exception as emm_err:
                    if hasattr(emm_err, "retry_after"):
                        await asyncio.sleep(getattr(emm_err, "retry_after") + 0.2)
                        await a.bot.edit_message_media(
                            inline_message_id=inline_message_id,
                            media=media_obj,
                            reply_markup=None,
                        )
                    else:
                        raise emm_err
                logging.info(f"Successfully replaced inline message {inline_message_id} with media!")
            except Exception as edit_err:
                logging.error(f"Failed to edit_message_media: {edit_err}")
                err_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🤖 Открыть бота", url="https://t.me/duo_play_date_bot?start=1")],
                    [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"retry_dl:{token}")],
                ])
                try:
                    await a.bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text="❌ <b>Не удалось прикрепить медиа в инлайн-сообщение.</b>\n\nПопробуйте ещё раз или отправьте ссылку напрямую боту 👇",
                        parse_mode="HTML",
                        reply_markup=err_kb,
                    )
                except Exception:
                    pass
    finally:
        _active_inline_jobs.discard(job_key)


@router.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult):
    logging.info(
        f"🎯 [INLINE CHOSEN] user={chosen.from_user.id} result_id={chosen.result_id} inline_msg={chosen.inline_message_id}"
    )
    if not chosen.inline_message_id:
        return
    rid = chosen.result_id
    if rid.startswith("art_v4_"):
        parts = rid.split("_")
        token = parts[2]
    else:
        token = rid.replace("ytv2_", "").replace("tok_", "").strip()
    await execute_inline_delivery(chosen.inline_message_id, token, chosen.from_user.id)


@router.callback_query(F.data.startswith("get_vid:"))
async def handle_get_vid_callback(callback: CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer()
        return
    token = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Скачиваю в чат...", show_alert=False)
    await execute_inline_delivery(callback.inline_message_id, token, callback.from_user.id)


@router.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery):
    await callback.answer("⏳ Медиа обрабатывается и появится в этом сообщении через пару секунд!", show_alert=False)


@router.callback_query(F.data.startswith("retry_dl:"))
async def handle_retry_callback(callback: CallbackQuery):
    a = _app()
    if not callback.inline_message_id:
        await callback.answer()
        return
    token = callback.data.split(":", 1)[1]
    await callback.answer("🔄 Перезапускаю загрузку...")
    try:
        url = a.get_dl_token_url(token)
        c_info = detect_content_info(url) if url else {}
        p_noun = c_info.get("label", "медиа")
        p_icon = c_info.get("icon", "⚡")
        await a.bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=(
                f"{p_icon} <b>Повторная загрузка {p_noun}...</b>\n"
                f"<code>[██░░░░░░░░░░] 15%</code>\n"
                f"<i>Пожалуйста, подождите ✨</i>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=f"⏳ Загрузка {p_noun}...", callback_data=f"get_vid:{token}")]]
            ),
        )
    except Exception:
        pass
    await execute_inline_delivery(callback.inline_message_id, token, callback.from_user.id)
