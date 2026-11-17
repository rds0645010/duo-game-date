"""
Единый пул HTTP-соединений (aiohttp.ClientSession) и унифицированный контракт MediaResult.
Избавляет от создания новой TCP/TLS-сессии на каждый запрос и гарантирует одинаковую
структуру ответа от всех модулей (TikTok, Instagram, YouTube, Music) для ЛС и инлайна.
"""

import asyncio
import logging
from typing import Optional, Dict, Any

import aiohttp

logger = logging.getLogger(__name__)

_session: Optional[aiohttp.ClientSession] = None
_session_loop: Optional[asyncio.AbstractEventLoop] = None


async def get_http_session() -> aiohttp.ClientSession:
    """
    Возвращает общий пул aiohttp.ClientSession для текущего event loop.
    Безопасно пересоздаёт сессию, если она закрыта или сменился event loop (например, в тестах).
    """
    global _session, _session_loop
    loop = asyncio.get_running_loop()

    if _session is not None and not _session.closed and _session_loop is loop:
        return _session

    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as e:
            logger.debug(f"Closing stale HTTP session notice: {e}")

    connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
    _session = aiohttp.ClientSession(connector=connector)
    _session_loop = loop
    return _session


async def close_http_session():
    """Корректно закрывает общий пул соединений при остановке бота."""
    global _session, _session_loop
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as e:
            logger.debug(f"Error closing HTTP session: {e}")
    _session = None
    _session_loop = None


def normalize_media_result(
    res: Optional[Dict[str, Any]],
    default_platform: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Нормализует словарь ответа загрузчиков к единому контракту MediaResult.
    Гарантирует наличие стандартных полей и корректные типы без потери доп. ключей.
    """
    if not res or not isinstance(res, dict):
        return None

    is_audio = bool(res.get("is_audio", False))
    is_album = bool(res.get("is_album", False))
    is_photo = bool(res.get("is_photo", False))
    is_video = bool(res.get("is_video", not (is_audio or is_photo or is_album)))

    normalized: Dict[str, Any] = {
        "file_path": res.get("file_path"),
        "direct_url": res.get("direct_url") or res.get("video_url"),
        "is_video": is_video,
        "is_photo": is_photo,
        "is_audio": is_audio,
        "is_album": is_album,
        "photos": list(res.get("photos") or []),
        "photo_urls": list(res.get("photo_urls") or []),
        "title": (res.get("title") or "").strip() or "Медиа",
        "author": (res.get("author") or "").strip(),
        "performer": (res.get("performer") or res.get("author") or "").strip(),
        "duration": int(res.get("duration") or 0),
        "filesize": int(res.get("filesize") or 0),
        "platform": (res.get("platform") or default_platform or "").strip().lower(),
        "music": res.get("music"),
        "music_info": res.get("music_info") if isinstance(res.get("music_info"), dict) else None,
        "low_quality_notice": bool(res.get("low_quality_notice", False)),
        "thumbnail_path": res.get("thumbnail_path"),
        "digg_count": int(res.get("digg_count") or 0),
        "comment_count": int(res.get("comment_count") or 0),
        "share_count": int(res.get("share_count") or 0),
        "original_url": res.get("original_url") or "",
    }

    # Сохраняем вспомогательные поля (например, fast_track, _bg_task, thumbnail, cover)
    for key, val in res.items():
        if key not in normalized:
            normalized[key] = val

    return normalized
