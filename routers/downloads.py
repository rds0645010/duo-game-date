"""
Модуль загрузки медиа в личных сообщениях и группах:
управление семафорами/очередями платформ, Fast-Track старт слайдшоу,
автоудаление временных файлов и фильтр чаттер-сообщений.
"""

import os
import html
import time
import uuid
import asyncio
import logging

from aiogram import Router, F, types
from aiogram.enums import ChatAction
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile

from downloader import (
    detect_platform,
    detect_content_info,
    extract_url,
    extract_all_media_urls,
    download_video,
    is_valid_mp4_file,
)
from . import app as _app
from .slideshow import make_slide_caption, build_slideshow_keyboard, preupload_slideshow_file_ids

logger = logging.getLogger(__name__)
router = Router(name="downloads")

_user_active_downloads = {}
_user_recent_links = {}
_user_last_msg_ts = {}
_user_last_download_ts = {}
_user_last_welcome_ts = {}

CHATTER_WORDS = {
    "привет", "хай", "ку", "hello", "hi", "спасибо", "спс", "спс)", "ок", "круто",
    "топ", "хаха", "ахах", "лол", "вай", "кайф", "норм", "класс", "ого", "ппц",
    "жесть", "👍", "🔥", "❤️", "😂", "ясно", "понял", "понятно", "супер", "от души",
}


def format_friendly_error(e: Exception, platform: str = "") -> str:
    """Преобразует технические ошибки в понятные объяснения для пользователя."""
    err_text = str(e).lower()

    if platform in ("spotify", "soundcloud", "music"):
        if "not found" in err_text or "not_found" in err_text or "spotify_track_not_found" in err_text:
            return (
                "🎵 <b>Трек не найден или недоступен для скачивания.</b>\n\n"
                "💡 <i>Попробуй найти этот трек по названию через команду:</i> <code>/music &lt;название&gt;</code>"
            )
        elif "confirm you’re not a bot" in err_text or "confirm you're not a bot" in err_text or "youtube_bot_detection" in err_text:
            return (
                "🎵 <b>Облачный сервер временно ограничил прямое подключение к аудиопотоку.</b>\n\n"
                "Попробуй отправить ссылку ещё раз через пару секунд или найти трек по названию через команду: <code>/music &lt;название&gt;</code>"
            )
        else:
            return (
                f"🎵 <b>Не удалось скачать аудиозапись.</b>\n\n"
                f"⚠️ Ошибка: <code>{html.escape(str(e)[:150])}</code>\n\n"
                f"💡 <i>Попробуй найти трек по названию через команду:</i> <code>/music &lt;название&gt;</code>"
            )
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
            "💡 <i>Администратор может задать <code>INSTAGRAM_SESSIONID</code> или команду <code>/cookies sessionid=...</code>!</i>"
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


async def process_video_download(message: types.Message, url: str):
    a = _app()
    clean_url = extract_url(url) or url
    platform = detect_platform(clean_url)
    logging.info(f"🚀 [СКАЧИВАНИЕ] Старт: {clean_url} (платформа: {platform}, от user: {message.from_user.id})")
    if not platform:
        await message.answer("⚠️ Не удалось определить платформу. Поддерживаются: TikTok, YouTube Shorts, Reels.")
        return

    user_id = message.from_user.id
    cached = a.get_cached_media(clean_url)
    if a.drop_poisoned_media_cache(clean_url, cached):
        cached = None
    if cached and ("/photo/" in clean_url or "/p/" in clean_url):
        logging.info(f"Bypassing cache for potential album: {clean_url}")
        cached = None
    if cached and cached.get("file_id"):
        plat_esc = html.escape(cached.get("platform", "Медиа").capitalize())
        f_id = cached["file_id"]
        m_type = cached.get("media_type", "video")
        a.record_user_download(user_id, clean_url, f_id, m_type, cached.get("title", ""), cached.get("platform", ""))

        if m_type == "photo":
            p_lines = []
            desc_line = a.format_description_line(cached.get("title") or "", is_photo=True)
            if desc_line:
                p_lines.append(desc_line)
            m_line = a.format_music_line(
                music_info=cached.get("music_info"), music=cached.get("music"), post_title=cached.get("title")
            )
            if m_line:
                p_lines.append(m_line)
            p_lines.append(f"📱 <b>Источник:</b> {plat_esc}\n✨ <i>Скачано через @duo_play_date_bot (мгновенно из кэша ⚡)</i>")
            try:
                await message.answer_photo(photo=f_id, caption="\n\n".join(p_lines), reply_markup=None, parse_mode="HTML")
                return
            except Exception as e:
                logging.warning(f"Failed to send cached photo file_id {f_id}: {e}")
        elif m_type == "audio":
            a_lines = []
            desc_line = a.format_description_line(cached.get("title") or "", is_photo=False, is_audio=True)
            if desc_line:
                a_lines.append(desc_line)
            a_lines.append(f"📱 <b>Источник:</b> {plat_esc}\n✨ <i>Скачано через @duo_play_date_bot (мгновенно из кэша ⚡)</i>")
            try:
                await message.answer_audio(
                    audio=f_id,
                    title=(cached.get("title") or "Трек")[:80],
                    performer=(cached.get("author") or "Исполнитель")[:50],
                    caption="\n\n".join(a_lines),
                    reply_markup=None,
                    parse_mode="HTML",
                )
                return
            except Exception as e:
                logging.warning(f"Failed to send cached audio file_id {f_id}: {e}")
        else:
            v_lines = []
            desc_line = a.format_description_line(cached.get("title") or "", is_photo=False)
            if desc_line:
                v_lines.append(desc_line)
            m_line = a.format_music_line(
                music_info=cached.get("music_info"), music=cached.get("music"), post_title=cached.get("title")
            )
            if m_line:
                v_lines.append(m_line)
            v_lines.append(f"📱 <b>Источник:</b> {plat_esc}\n✨ <i>Скачано через @duo_play_date_bot без водяных знаков и рекламы (мгновенно из кэша ⚡)</i>")
            try:
                await message.answer_video(video=f_id, caption="\n\n".join(v_lines), reply_markup=None, parse_mode="HTML")
                return
            except Exception as e:
                logging.warning(f"Failed to send cached video file_id {f_id}: {e}")

    is_vip = str(user_id) in a.ADMIN_IDS
    c_info = detect_content_info(clean_url)
    target_platform = c_info.get("platform") or detect_platform(clean_url) or "youtube"

    if target_platform == "tiktok":
        sem, lock, counter_attr = a.TIKTOK_SEMAPHORE, a.tiktok_lock, "tiktok_waiting_count"
    elif target_platform == "instagram":
        sem, lock, counter_attr = a.INSTAGRAM_SEMAPHORE, a.instagram_lock, "instagram_waiting_count"
    elif target_platform in ("spotify", "soundcloud", "music"):
        sem, lock, counter_attr = a.MUSIC_SEMAPHORE, a.music_lock, "music_waiting_count"
    else:
        sem, lock, counter_attr = a.YOUTUBE_SEMAPHORE, a.youtube_lock, "youtube_waiting_count"

    p_label = c_info.get("label", "медиа")
    p_icon = c_info.get("icon", "⚡")
    status_msg = None
    queue_acquired = False
    res = None

    try:
        if sem.locked() and not is_vip:
            async with lock:
                pos = int(getattr(a, counter_attr, 0)) + 1
                setattr(a, counter_attr, pos)
            status_msg = await message.answer(
                f"⏳ <b>В очереди на загрузку ({p_label})...</b>\n"
                f"Твоя позиция: {pos}. Ожидаем завершения предыдущих загрузок 🚀",
                parse_mode="HTML",
            )
        else:
            status_msg = await message.answer(
                f"{p_icon} <b>Загружаю {p_label}...</b>\n"
                f"<code>[██░░░░░░░░░░] 15%</code>\n"
                f"🔍 Получаю данные публикации...",
                parse_mode="HTML",
            )

        if is_vip:
            queue_acquired = True
            await a.safe_edit_status(
                status_msg,
                f"👑 <b>VIP приоритет активен!</b>\n<code>[███░░░░░░░░░] 25%</code>\nЗагружаю {p_label} вне очереди...",
            )
        else:
            await sem.acquire()
            queue_acquired = True
            async with lock:
                cur_cnt = int(getattr(a, counter_attr, 0))
                if cur_cnt > 0:
                    setattr(a, counter_attr, cur_cnt - 1)
            await a.safe_edit_status(
                status_msg,
                f"{p_icon} <b>Загружаю {p_label}...</b>\n<code>[███░░░░░░░░░] 25%</code>\nПодключаюсь и начинаю скачивание...",
            )

        progress_cb = a.create_progress_reporter(status_msg, c_info)
        res = await download_video(clean_url, user_id, progress_callback=progress_cb)
        if not res or not isinstance(res, dict):
            await a.safe_edit_status(
                status_msg,
                f"❌ <b>Не удалось загрузить {p_label}.</b>\nВозможно, публикация удалена или автор ограничил доступ.",
            )
            return

        # 1. Фото-слайдшоу / карусель (включая Fast-Track)
        if res.get("is_album") and (res.get("photos") or res.get("photo_urls")):
            photos = res.get("photos") or []
            photo_urls = res.get("photo_urls") or []
            total_slides = max(len(photos), len(photo_urls))
            await a.safe_edit_status(
                status_msg,
                f"📸 <b>Подготавливаю слайдшоу ({total_slides} слайдов)...</b>\n"
                f"<code>[████████████] 100%</code>\nОтправляю в Telegram...",
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
                "original_url": clean_url,
                "music": res.get("music"),
                "music_info": res.get("music_info"),
                "digg_count": res.get("digg_count", 0),
                "comment_count": res.get("comment_count", 0),
                "chat_id": message.chat.id,
                "created_at": time.time(),
                "_bg_task": res.get("_bg_task"),
            }
            a.save_slideshow_session_db(sid, a._slideshow_cache[sid])

            first_media = FSInputFile(photos[0]) if (photos and os.path.exists(photos[0])) else photo_urls[0]
            caption = make_slide_caption(a._slideshow_cache[sid], 0, total_slides)
            kb = build_slideshow_keyboard(sid, 0, total_slides, a._slideshow_cache[sid])
            is_vid = (photos[0].lower().endswith((".mp4", ".mov", ".m4v")) if (photos and os.path.exists(photos[0])) else False) or str(first_media).lower().endswith((".mp4", ".mov", ".m4v"))
            if is_vid:
                sent = await message.answer_video(video=first_media, caption=caption, reply_markup=kb, parse_mode="HTML")
                if sent and sent.video:
                    a._slideshow_cache[sid]["file_ids"][0] = sent.video.file_id
                    a.save_slideshow_session_db(sid, a._slideshow_cache[sid])
            else:
                sent = await message.answer_photo(photo=first_media, caption=caption, reply_markup=kb, parse_mode="HTML")
                if sent and sent.photo:
                    a._slideshow_cache[sid]["file_ids"][0] = sent.photo[-1].file_id
                    a.save_slideshow_session_db(sid, a._slideshow_cache[sid])

            try:
                await status_msg.delete()
            except Exception:
                pass
            if total_slides > 1:
                a.track_background_task(preupload_slideshow_file_ids(sid))
            return

        file_path = res.get("file_path")
        if not file_path or not os.path.exists(file_path):
            await a.safe_edit_status(
                status_msg,
                "❌ Не удалось загрузить видео файл. Возможно, видео удалено или автор ограничил доступ.",
            )
            return

        file_size = os.path.getsize(file_path)
        platform_names = {
            "tiktok": "TikTok",
            "youtube": "YouTube Shorts",
            "instagram": "Instagram Reels",
            "spotify": "Spotify",
            "soundcloud": "SoundCloud",
        }
        plat_str = platform_names.get(res.get("platform"), "Медиа")
        title_raw = (res.get("title") or "Аудиозапись")[:200]
        author_raw = (res.get("performer") or res.get("author") or "")[:50]
        title_esc = html.escape(title_raw)
        author_esc = html.escape(author_raw)

        # 2. Аудио (Spotify, SoundCloud, музыка, подкаст > 50MB)
        if res.get("is_audio"):
            try:
                await a.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VOICE)
            except Exception:
                pass
            file_size_mb = round(file_size / (1024 * 1024), 1)
            await a.safe_edit_status(
                status_msg,
                f"🎵 <b>Отправляю трек в Telegram ({file_size_mb} МБ)...</b>\n<code>[████████████] 100%</code>",
            )
            is_music_source = res.get("platform") in ("spotify", "soundcloud", "music")
            performer = res.get("performer") or author_raw or "Исполнитель"
            track_title = res.get("title") or title_raw or "Трек"
            if is_music_source:
                src_name = "Spotify" if res.get("platform") == "spotify" else ("SoundCloud" if res.get("platform") == "soundcloud" else "Музыка")
                audio_caption = (
                    f"🎧 <b>{html.escape(track_title)}</b> — {html.escape(performer)}\n\n"
                    f"📱 <b>Источник:</b> {src_name}\n"
                    f"✨ <i>Скачано через @duo_play_date_bot</i>"
                )
            else:
                audio_caption = (
                    f"🎧 <b>{title_esc}</b>\n\n"
                    f"📱 <b>Источник:</b> {plat_str} (Аудио / Подкаст)\n"
                    f"ℹ️ <i>(Ролик больше 50 МБ — видео-лимит Telegram, поэтому отправлен как чистая аудиозапись для удобного прослушивания)</i>\n"
                    f"✨ <i>Скачано через @duo_play_date_bot</i>"
                )
            thumb_path = res.get("thumbnail_path")
            thumb_input = FSInputFile(thumb_path) if (thumb_path and os.path.exists(thumb_path)) else None
            sent = await message.answer_audio(
                audio=FSInputFile(file_path),
                caption=audio_caption,
                title=track_title[:80],
                performer=performer[:50],
                duration=int(res.get("duration", 0)),
                thumbnail=thumb_input,
                reply_markup=None,
                parse_mode="HTML",
            )
            if sent and sent.audio:
                a.save_cached_media(
                    clean_url,
                    sent.audio.file_id,
                    "audio",
                    track_title,
                    res.get("platform", "music"),
                    performer,
                    music=res.get("music"),
                    music_info=res.get("music_info"),
                )
                a._safe_remove(file_path)
                a._safe_remove(thumb_path)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # Валидация по сигнатурам: не даём видео уйти как photo, а Reels — как картинка
        if is_valid_mp4_file(file_path, allow_audio=False):
            res["is_photo"] = False
            res["is_video"] = True
        elif res.get("is_photo") and a.is_instagram_reel_url(clean_url):
            raise Exception("instagram_video_unavailable")

        # 3. Одиночное фото
        if res.get("is_photo"):
            await a.safe_edit_status(
                status_msg,
                "📸 <b>Отправляю фото в Telegram...</b>\n<code>[████████████] 100%</code>",
            )
            try:
                await a.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_PHOTO)
            except Exception:
                pass
            plat_disp = "Instagram" if res.get("platform") == "instagram" else ("TikTok" if res.get("platform") == "tiktok" else "Медиа")
            p_lines = []
            desc_line = a.format_description_line(res.get("title") or "", is_photo=True)
            if desc_line:
                p_lines.append(desc_line)
            m_line = a.format_music_line(
                music_info=res.get("music_info"), music=res.get("music"), post_title=res.get("title")
            )
            if m_line:
                p_lines.append(m_line)
            p_lines.append(f"📱 <b>Источник:</b> {plat_disp}\n✨ <i>Скачано через @duo_play_date_bot</i>")
            sent = await message.answer_photo(
                photo=FSInputFile(file_path),
                caption="\n\n".join(p_lines),
                reply_markup=None,
                parse_mode="HTML",
            )
            if sent and sent.photo:
                f_id = sent.photo[-1].file_id
                a.save_cached_media(
                    clean_url, f_id, "photo", res.get("title", ""), res.get("platform", ""), res.get("author", ""),
                    music=res.get("music"), music_info=res.get("music_info"),
                )
                a.record_user_download(user_id, clean_url, f_id, "photo", res.get("title", ""), res.get("platform", ""))
                a._safe_remove(file_path)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # Проверка размера Telegram Bot API 50 МБ
        if file_size > 50 * 1024 * 1024:
            await a.safe_edit_status(
                status_msg,
                f"⚠️ <b>Файл слишком большой ({round(file_size / (1024*1024), 1)} МБ)</b>\n\n"
                f"Telegram Bot API не позволяет отправлять ботам видео больше 50 МБ.\n"
                f"Попробуй более короткое видео или шортс.",
            )
            return

        file_size_mb = round(file_size / (1024 * 1024), 1)
        await a.safe_edit_status(
            status_msg,
            f"🎬 <b>Отправляю чистое видео ({file_size_mb} МБ)...</b>\n<code>[████████████] 100%</code>",
        )
        try:
            await a.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
        except Exception:
            pass

        v_lines = []
        desc_line = a.format_description_line(res.get("title") or "", is_photo=False)
        if desc_line:
            v_lines.append(desc_line)
        m_line = a.format_music_line(
            music_info=res.get("music_info"), music=res.get("music"), post_title=res.get("title")
        )
        if m_line:
            v_lines.append(m_line)
        meta = f"📱 <b>Источник:</b> {plat_str}"
        if author_esc and author_esc.lower() not in ("tiktok", "instagram", "media", "видео", "creator", "unknown"):
            meta += f" | {author_esc}"
        v_lines.append(meta)
        v_lines.append("✨ <i>Скачано через @duo_play_date_bot без водяных знаков и рекламы</i>")
        if res.get("low_quality_notice"):
            v_lines.append("<i>(ℹ️ Качество сжато до 360p, так как исходное видео превышало лимиты)</i>")
        caption = "\n\n".join(v_lines)

        video_input = FSInputFile(file_path)
        try:
            sent = await message.answer_video(
                video=video_input,
                caption=caption,
                reply_markup=None,
                parse_mode="HTML",
                supports_streaming=True,
            )
        except Exception as html_send_err:
            logging.warning(f"HTML caption send error: {html_send_err}, sending plain caption...")
            sent = await message.answer_video(
                video=video_input,
                caption=f"🎬 {title_raw[:100]}\n✨ Скачано через @duo_play_date_bot",
                reply_markup=None,
                supports_streaming=True,
            )

        if sent and sent.video and sent.video.file_id:
            v_id = sent.video.file_id
            a.save_cached_media(
                clean_url, v_id, "video", res.get("title", ""), res.get("platform", ""), res.get("author", ""),
                music=res.get("music"), music_info=res.get("music_info"),
            )
            a.record_user_download(user_id, clean_url, v_id, "video", res.get("title", ""), res.get("platform", ""))
            a._safe_remove(file_path)

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        a.record_error("process_video_download", e, clean_url)
        logging.error(f"Download processing failed for {clean_url}: {e}", exc_info=True)
        msg = format_friendly_error(e, platform=target_platform)
        if status_msg:
            await a.safe_edit_status(status_msg, msg)
        else:
            await message.answer(msg, parse_mode="HTML")
    finally:
        if queue_acquired and not is_vip:
            sem.release()
        if res and isinstance(res, dict) and not res.get("is_album"):
            for k in ("file_path", "thumbnail_path"):
                fp = res.get(k)
                if fp and os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception as del_err:
                        logging.warning(f"Could not remove temp file {fp}: {del_err}")


@router.message(F.text | F.caption)
async def handle_text_messages(message: types.Message):
    a = _app()
    raw_text = message.text or message.caption or ""
    user_id = message.from_user.id if message.from_user else 0
    u_name = (message.from_user.username or message.from_user.first_name or "no_user") if message.from_user else "unknown"
    logging.info(f"📩 [ЛС от {user_id} (@{u_name})]: {raw_text[:120]}")
    if message.chat and message.chat.type == "private" and message.from_user:
        a.save_user(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )

    now = time.time()
    media_urls = extract_all_media_urls(raw_text)

    if media_urls:
        url = media_urls[0]
        clean_u = url.split("?")[0].strip().rstrip("/")

        user_links = _user_recent_links.setdefault(user_id, {})
        for k in list(user_links.keys()):
            if now - user_links[k] > 300:
                user_links.pop(k, None)

        if clean_u in user_links and (now - user_links[clean_u] < 60):
            logging.info(f"⏭ [ДУБЛИКАТ] Пропускаю повторную ссылку от {user_id}: {clean_u}")
            return

        if _user_active_downloads.get(user_id, 0) >= 1 and (now - _user_last_msg_ts.get(user_id, 0) < 5):
            logging.info(f"⏭ [ОЧЕРЕДЬ] Пропускаю ссылку подряд от {user_id}: {clean_u}")
            return

        _user_last_msg_ts[user_id] = now
        _user_recent_links[user_id][clean_u] = now
        _user_last_download_ts[user_id] = now

        _user_active_downloads[user_id] = _user_active_downloads.get(user_id, 0) + 1
        try:
            await process_video_download(message, url)
        finally:
            _user_active_downloads[user_id] = max(0, _user_active_downloads.get(user_id, 0) - 1)
        return

    if now - _user_last_download_ts.get(user_id, 0) < 900:
        return
    text_strip = raw_text.strip().lower()
    if text_strip in CHATTER_WORDS or len(text_strip) <= 6:
        return
    if now - _user_last_welcome_ts.get(user_id, 0) < 43200:
        return

    _user_last_welcome_ts[user_id] = now
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎮 Открыть лобби DUO ✨", web_app=WebAppInfo(url=a.get_app_url()))]]
    )
    await message.answer(
        "👾 **Я умею две вещи:**\n\n"
        "1. 📥 **Скачивать видео без водяных знаков и рекламы** — просто отправь мне ссылку на **TikTok, YouTube Shorts или Instagram Reels**!\n\n"
        "2. 🎮 **Организовывать совместные игры в DUO** — открой лобби по кнопке ниже и пригласи напарника в пару кликов!",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
