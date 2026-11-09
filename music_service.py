"""
Модуль для поиска и скачивания музыки (Spotify, YouTube Music, SoundCloud, обычный поиск по названию).
Поддерживает:
- Поиск треков по названию с выдачей интерактивного списка (топ-5);
- Распознавание ссылок Spotify (track, album, playlist) через публичный OEmbed;
- Скачивание чистого аудиопотока в MP3 с тегами исполнителя, названия и обложкой (album cover);
- Интеграцию с живым прогресс-баром бота.
"""

import os
import re
import time
import uuid
import shutil
import logging
import asyncio
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import yt_dlp

logger = logging.getLogger("music_service")

DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# FFmpeg detection
FFMPEG_PATH = None
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass
if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = shutil.which("ffmpeg")

# Regexes
SPOTIFY_TRACK_REGEX = re.compile(
    r'https?://(?:open\.)?spotify\.com/(?:intl-[a-z]+/)?track/([a-zA-Z0-9]+)',
    re.IGNORECASE
)
SPOTIFY_ALBUM_REGEX = re.compile(
    r'https?://(?:open\.)?spotify\.com/(?:intl-[a-z]+/)?(album|playlist)/([a-zA-Z0-9]+)',
    re.IGNORECASE
)
SOUNDCLOUD_REGEX = re.compile(
    r'https?://(?:www\.)?soundcloud\.com/[\w\-]+/[\w\-]+',
    re.IGNORECASE
)

# Кеш сессий поиска (search_id -> {tracks, query, created_at})
_search_sessions: Dict[str, Dict[str, Any]] = {}


def is_spotify_url(text: str) -> bool:
    if not text:
        return False
    return bool(SPOTIFY_TRACK_REGEX.search(text) or SPOTIFY_ALBUM_REGEX.search(text))


def is_music_url(text: str) -> bool:
    if not text:
        return False
    return bool(
        SPOTIFY_TRACK_REGEX.search(text)
        or SPOTIFY_ALBUM_REGEX.search(text)
        or SOUNDCLOUD_REGEX.search(text)
    )


def parse_artist_and_title(raw_title: str, uploader: str = "") -> Tuple[str, str]:
    """
    Очищает заголовок от мусорных тегов YouTube (Official Video, 4K, Remastered и т.д.)
    и аккуратно разделяет на исполнителя и название трека.
    """
    cleaned = raw_title or ""
    # Удаляем скобки с мусором: [Official Video], (4K UPGRADE), (Lyrics), (Audio), etc.
    cleaned = re.sub(r'(?i)\[[^\]]*?(?:4k|hd|upgrade|remaster|video|audio|clip|hq)[^\]]*?\]', '', cleaned)
    cleaned = re.sub(r'(?i)\([^)]*?(?:official|video|audio|lyrics?|remaster(?:ed)?|visualizer|hq|clip)[^)]*?\)', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -[](){}|')

    if " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        artist = parts[0].strip(' -[](){}|')
        title = parts[1].strip(' -[](){}|')
    elif uploader:
        artist = uploader.replace(" - Topic", "").replace("VEVO", "").strip()
        title = cleaned
    else:
        artist = "Исполнитель"
        title = cleaned

    if not title:
        title = raw_title or "Аудиозапись"
    if not artist:
        artist = "Неизвестный исполнитель"

    return artist, title


async def fetch_spotify_oembed(url: str) -> Optional[Dict[str, Any]]:
    """
    Получает метаданные трека из Spotify через публичный OEmbed без API-ключей.
    """
    oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.warning(f"Spotify oEmbed fetch failed: {e}")
    return None


def _search_youtube_tracks_sync(query: str, limit: int = 5, proxy_url: str = None) -> List[Dict[str, Any]]:
    """
    Синхронный поиск треков через ytsearch с extract_flat.
    Автоматически пробует с прокси и напрямую, чтобы гарантировать результат.
    """
    search_spec = f"ytsearch{limit}:{query}"
    proxies_to_try = [proxy_url] if proxy_url else ['']
    if proxy_url and '' not in proxies_to_try:
        proxies_to_try.append('')

    results = []
    last_err = None

    for p in proxies_to_try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True,
            'proxy': p,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_spec, download=False)
                entries = info.get('entries') or []
                for e in entries:
                    if not e or not e.get('id'):
                        continue
                    vid = e.get('id')
                    raw_ttl = e.get('title') or "Без названия"
                    uploader = e.get('uploader') or ""
                    dur_sec = int(e.get('duration') or 0)
                    # Отсекаем стримы и длинные подкасты (> 20 минут)
                    if dur_sec > 1200:
                        continue
                    dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else "?:??"
                    artist, title = parse_artist_and_title(raw_ttl, uploader)
                    thumb = e.get('thumbnail') or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                    results.append({
                        "id": vid,
                        "title": title,
                        "artist": artist,
                        "raw_title": raw_ttl,
                        "duration_sec": dur_sec,
                        "duration_str": dur_str,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "thumbnail_url": thumb,
                    })
                if results:
                    return results
        except Exception as e:
            last_err = e
            logger.warning(f"Track search warning for proxy '{p}': {e}")

    if not results and last_err:
        logger.error(f"Track search error for '{query}': {last_err}")
    return results


async def search_tracks(query: str, limit: int = 5, proxy_url: str = None) -> List[Dict[str, Any]]:
    """Асинхронный поиск треков по названию."""
    return await asyncio.to_thread(_search_youtube_tracks_sync, query, limit, proxy_url)


def save_search_session(query: str, tracks: List[Dict[str, Any]]) -> str:
    """Сохраняет результаты поиска в кеш с TTL."""
    now = time.time()
    for k in list(_search_sessions.keys()):
        if now - _search_sessions[k].get("created_at", 0) > 1800:
            _search_sessions.pop(k, None)
    sid = uuid.uuid4().hex[:8]
    _search_sessions[sid] = {
        "query": query,
        "tracks": tracks,
        "created_at": now
    }
    return sid


def get_search_track(sid: str, index: int) -> Optional[Dict[str, Any]]:
    """Получает трек по search_id и порядковому номеру."""
    sess = _search_sessions.get(sid)
    if not sess:
        return None
    tracks = sess.get("tracks", [])
    if 0 <= index < len(tracks):
        return tracks[index]
    return None


async def download_thumbnail_file(url: str, dest_path: str) -> Optional[str]:
    """Скачивает обложку трека для вставки в аудио Telegram."""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    with open(dest_path, "wb") as f:
                        f.write(await resp.read())
                    return dest_path
    except Exception as e:
        logger.debug(f"Failed to download audio thumbnail: {e}")
    return None


def _download_audio_ytdlp_sync(
    url: str,
    output_prefix: str,
    progress_callback=None,
    event_loop=None,
    proxy_url: str = None
) -> Dict[str, Any]:
    """
    Синхронное скачивание чистого аудиопотока с извлечением в MP3.
    Автоматически пробует через прокси и напрямую.
    """
    out_tmpl = f"{output_prefix}.%(ext)s"
    proxies_to_try = [proxy_url] if proxy_url else ['']
    if proxy_url and '' not in proxies_to_try:
        proxies_to_try.append('')

    last_error = None
    info = None

    for p in proxies_to_try:
        ydl_opts = {
            'outtmpl': out_tmpl,
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'max_filesize': 50 * 1024 * 1024,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'socket_timeout': 25,
            'retries': 2,
            'extractor_args': {'youtube': {'player_client': ['visionos']}},
            'proxy': p,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            },
        }

        if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
            ydl_opts['ffmpeg_location'] = FFMPEG_PATH
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]

        if progress_callback and event_loop:
            def _ytdlp_progress_hook(d):
                try:
                    st = d.get('status')
                    if st == 'downloading':
                        dl = d.get('downloaded_bytes') or 0
                        tot = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                        spd = d.get('speed') or 0
                        eta = d.get('eta') or 0
                        if tot > 0:
                            asyncio.run_coroutine_threadsafe(
                                progress_callback(dl, tot, spd, eta),
                                event_loop
                            )
                    elif st == 'finished':
                        asyncio.run_coroutine_threadsafe(
                            progress_callback(-1, -1, 0, 0),
                            event_loop
                        )
                except Exception:
                    pass
            ydl_opts['progress_hooks'] = [_ytdlp_progress_hook]

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if info:
                break
        except Exception as dl_err:
            last_error = dl_err
            logger.warning(f"Audio download attempt failed with proxy '{p}': {dl_err}")

    if not info and last_error:
        raise last_error

    candidates = [
        f"{output_prefix}.{ext}"
        for ext in ("mp3", "m4a", "opus", "aac", "ogg", "webm")
    ]
    actual_file = None
    for cand in candidates:
        if os.path.exists(cand) and os.path.getsize(cand) > 1024:
            actual_file = cand
            break

    if not actual_file:
        # Поиск по префиксу
        base_dir = os.path.dirname(output_prefix) or DOWNLOAD_DIR
        base_name = os.path.basename(output_prefix)
        for fname in os.listdir(base_dir):
            if fname.startswith(base_name) and not fname.endswith((".jpg", ".png", ".webp")):
                full_p = os.path.join(base_dir, fname)
                if os.path.getsize(full_p) > 1024:
                    actual_file = full_p
                    break

    if not actual_file:
        raise Exception("audio_download_failed")

    return {
        "file_path": actual_file,
        "raw_title": info.get("title", ""),
        "uploader": info.get("uploader", ""),
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail"),
    }


async def download_track(
    url_or_info: Any,
    output_prefix: str,
    custom_artist: str = None,
    custom_title: str = None,
    custom_thumb_url: str = None,
    progress_callback = None,
    proxy_url: str = None
) -> Dict[str, Any]:
    """
    Полный цикл скачивания трека:
    - Извлекает аудиопоток;
    - Скачивает обложку (из Spotify или YouTube);
    - Формирует метаданные исполнителя и названия.
    """
    loop = asyncio.get_running_loop()

    if isinstance(url_or_info, dict):
        url = url_or_info.get("url")
        custom_artist = custom_artist or url_or_info.get("artist")
        custom_title = custom_title or url_or_info.get("title")
        custom_thumb_url = custom_thumb_url or url_or_info.get("thumbnail_url")
    else:
        url = str(url_or_info)

    res = await asyncio.to_thread(
        _download_audio_ytdlp_sync,
        url,
        output_prefix,
        progress_callback,
        loop,
        proxy_url
    )

    raw_ttl = res.get("raw_title") or ""
    uploader = res.get("uploader") or ""
    parsed_artist, parsed_title = parse_artist_and_title(raw_ttl, uploader)

    final_artist = custom_artist or parsed_artist
    final_title = custom_title or parsed_title
    duration = res.get("duration", 0)

    # Скачиваем обложку
    thumb_url = custom_thumb_url or res.get("thumbnail")
    thumb_path = f"{output_prefix}_cover.jpg"
    final_thumb = await download_thumbnail_file(thumb_url, thumb_path)

    return {
        "file_path": res["file_path"],
        "title": final_title,
        "performer": final_artist,
        "duration": duration,
        "thumbnail_path": final_thumb,
        "platform": "spotify" if "spotify.com" in url else "music",
    }
