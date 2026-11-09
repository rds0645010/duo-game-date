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
    cleaned = re.sub(r'(?i)\[[^\]]*?(?:4k|hd|upgrade|remaster|video|audio|clip|hq|visualizer)[^\]]*?\]', '', cleaned)
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


def parse_spotify_html_meta(html_text: str) -> Dict[str, Any]:
    """
    Парсит метаданные трека из HTML Spotify (Embed, OpenGraph, JSON-LD, __NEXT_DATA__).
    Надежно извлекает исполнителя (artist), название (title), обложку и длительность.
    """
    import json
    artist = None
    title = None
    thumb = None
    duration_sec = 0

    if not html_text:
        return {"title": None, "artist": None, "thumbnail_url": None, "duration": 0}

    # 1. __NEXT_DATA__ (наиболее точные структурированные данные Spotify Embed)
    m_next = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html_text, re.DOTALL)
    if m_next:
        try:
            nd = json.loads(m_next.group(1))
            page_props = nd.get("props", {}).get("pageProps", {})
            entity_data = page_props.get("state", {}).get("data", {})
            entity = entity_data.get("entity") or entity_data.get("track") or entity_data
            if isinstance(entity, dict):
                t_cand = entity.get("name") or entity.get("title")
                if t_cand and t_cand != "Sorry, that’s not currently available.":
                    title = title or t_cand
                artists = entity.get("artists", []) or entity_data.get("artists", [])
                if artists and isinstance(artists, list):
                    art_names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
                    if art_names:
                        artist = artist or ", ".join(art_names)
                dur_ms = entity.get("duration") or entity.get("duration_ms") or 0
                if dur_ms:
                    duration_sec = int(dur_ms / 1000)
                cover_sources = entity.get("coverArt", {}).get("sources", [])
                if cover_sources and isinstance(cover_sources, list):
                    thumb = thumb or cover_sources[0].get("url")
        except Exception as e:
            logger.debug(f"__NEXT_DATA__ parse notice: {e}")

    # 2. JSON-LD schema.org (MusicRecording)
    m_json = re.search(r'<script type="application/ld\+json">(.*?)</script>', html_text, re.DOTALL)
    if m_json:
        try:
            jd = json.loads(m_json.group(1))
            if isinstance(jd, dict):
                title = title or jd.get("name")
                by_artist = jd.get("byArtist", [])
                if by_artist and isinstance(by_artist, list):
                    art_names = [a.get("name") for a in by_artist if isinstance(a, dict) and a.get("name")]
                    if art_names:
                        artist = artist or ", ".join(art_names)
                elif isinstance(by_artist, dict) and by_artist.get("name"):
                    artist = artist or by_artist.get("name")
        except Exception as e:
            logger.debug(f"JSON-LD parse notice: {e}")

    # 3. OpenGraph og:title и og:description
    og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
    if og_title and og_title.group(1) not in ("Listening is everything", "Spotify"):
        title = title or og_title.group(1)

    tw_artist = re.search(r'<meta (?:name|property)="(?:twitter:audio:artist_name|music:musician)" content="([^"]+)"', html_text)
    if tw_artist and tw_artist.group(1):
        artist = artist or tw_artist.group(1)

    og_desc = re.search(r'<meta property="og:description" content="([^"]+)"', html_text)
    if og_desc and not artist:
        # Spotify OpenGraph description: "VLDONE · Не пробил · Song · 2025" или "VLDONE · Song · 2025"
        desc = og_desc.group(1)
        if "·" in desc or "•" in desc:
            sep = "·" if "·" in desc else "•"
            parts = [p.strip() for p in desc.split(sep)]
            if parts and parts[0] not in ("Spotify", "Song", "Single", "Album"):
                artist = parts[0]

    og_image = re.search(r'<meta property="og:image" content="([^"]+)"', html_text)
    if og_image:
        thumb = thumb or og_image.group(1)

    return {
        "title": title,
        "artist": artist,
        "thumbnail_url": thumb,
        "duration": duration_sec,
    }


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


async def fetch_spotify_metadata(url: str, proxy_url: str = None) -> Dict[str, Any]:
    """
    Универсальное извлечение полных метаданных трека из Spotify (исполнитель, название, обложка).
    Использует многоступенчатый каскад: Embed HTML -> Track Web HTML -> OEmbed fallback.
    """
    track_id = None
    m_tr = SPOTIFY_TRACK_REGEX.search(url)
    if m_tr:
        track_id = m_tr.group(1)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }

    proxies_to_try = [None]
    if proxy_url:
        proxies_to_try.insert(0, proxy_url)

    result_meta = {"title": None, "artist": None, "thumbnail_url": None, "duration": 0}

    # 1. Попытка запросить Spotify Embed HTML
    if track_id:
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        for p in proxies_to_try:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(embed_url, headers=headers, proxy=p, timeout=aiohttp.ClientTimeout(total=7)) as resp:
                        if resp.status == 200:
                            html_text = await resp.text()
                            parsed = parse_spotify_html_meta(html_text)
                            if parsed.get("title") and parsed.get("artist"):
                                result_meta.update(parsed)
                                break
            except Exception as e:
                logger.debug(f"Spotify embed fetch attempt failed (proxy={p}): {e}")

    # 2. Если исполнитель еще не найден — пробуем основную страницу трека
    if track_id and not result_meta.get("artist"):
        direct_url = f"https://open.spotify.com/track/{track_id}"
        for p in proxies_to_try:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(direct_url, headers=headers, proxy=p, timeout=aiohttp.ClientTimeout(total=7)) as resp:
                        if resp.status == 200:
                            html_text = await resp.text()
                            parsed = parse_spotify_html_meta(html_text)
                            for k, v in parsed.items():
                                if v and not result_meta.get(k):
                                    result_meta[k] = v
                            if result_meta.get("artist") and result_meta.get("title"):
                                break
            except Exception as e:
                logger.debug(f"Spotify direct page fetch failed (proxy={p}): {e}")

    # 3. Fallback через публичный OEmbed (если нет названия или обложки)
    if not result_meta.get("title") or not result_meta.get("thumbnail_url"):
        o_data = await fetch_spotify_oembed(url)
        if o_data:
            if not result_meta.get("title") and o_data.get("title"):
                result_meta["title"] = o_data.get("title")
            if not result_meta.get("thumbnail_url") and o_data.get("thumbnail_url"):
                result_meta["thumbnail_url"] = o_data.get("thumbnail_url")

    # 4. Если название содержит разделитель "Исполнитель - Трек"
    if result_meta.get("title") and not result_meta.get("artist"):
        t_raw = result_meta["title"]
        if " - " in t_raw:
            parts = t_raw.split(" - ", 1)
            result_meta["artist"] = parts[0].strip()
            result_meta["title"] = parts[1].strip()

    return result_meta


def score_track_entry(entry: Dict[str, Any], target_artist: str = "", target_title: str = "") -> int:
    """
    Интеллектуальная система оценки релевантности найденного аудио:
    - Отсекает короткие звуковые мем-эффекты (< 30 секунд);
    - Отсекает стримы и подкасты (> 20 минут);
    - Начисляет высокие баллы за точное совпадение исполнителя и названия;
    - Предпочитает стандартную длительность музыкальных треков (1:15 - 6:00);
    - Штрафует нарезки ("1 hour", "bass boosted", "slowed", "пародия").
    """
    title = (entry.get("title") or "").lower()
    uploader = (entry.get("uploader") or "").lower()
    dur = int(entry.get("duration") or 0)

    # Жесткое вето: менее 30 секунд (мемы/звуки) или более 20 минут
    if 0 < dur < 30:
        return -1000
    if dur > 1200:
        return -1000

    score = 0

    clean_tit = re.sub(r'[^\w\s]', '', title).strip()
    clean_uploader = re.sub(r'[^\w\s]', '', uploader).strip()
    clean_target = re.sub(r'[^\w\s]', '', target_title).lower().strip() if target_title else ""
    clean_artist = re.sub(r'[^\w\s]', '', target_artist).lower().strip() if target_artist else ""

    # 1. Совпадение исполнителя
    if clean_artist:
        if clean_artist == clean_uploader:
            score += 120
        elif clean_artist in clean_uploader:
            score += 90
        elif clean_artist in clean_tit:
            score += 70

    # 2. Совпадение названия
    if clean_target:
        if clean_tit == clean_target:
            score += 150  # Точное совпадение названия трека
        elif clean_artist and clean_tit == f"{clean_artist} {clean_target}":
            score += 140
        elif clean_target in clean_tit:
            score += 60

    # 3. Идеальный диапазон длительности для музыки (75с - 360с = 1:15 - 6:00)
    if 75 <= dur <= 360:
        score += 30
    elif 45 <= dur <= 600:
        score += 10

    # 4. Штраф за мусорные модификации (если пользователь явно их не искал)
    bad_keywords = ["1 hour", "1 час", "10 hours", "bass boosted", "slowed", "reverb", "parody", "пародия"]
    for bad_w in bad_keywords:
        if bad_w in title and (not clean_target or bad_w not in clean_target):
            score -= 40

    return score


def _search_youtube_tracks_sync(
    query: str,
    limit: int = 5,
    target_artist: str = "",
    target_title: str = "",
    proxy_url: str = None
) -> List[Dict[str, Any]]:
    """
    Синхронный поиск треков через ytsearch с ранжированием по релевантности.
    Автоматически пробует с прокси и напрямую.
    """
    if not target_artist and not target_title:
        if " - " in query:
            p_art, p_tit = query.split(" - ", 1)
            target_artist = p_art.strip()
            target_title = p_tit.strip()
        else:
            target_title = query.strip()

    # Ищем с запасом кандидатов (10-12), чтобы качественно отфильтровать мусор и мемы
    search_limit = max(limit * 2, 12)
    search_spec = f"ytsearch{search_limit}:{query}"
    proxies_to_try = [proxy_url] if proxy_url else ['']
    if proxy_url and '' not in proxies_to_try:
        proxies_to_try.append('')

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
                scored_entries = []

                for e in entries:
                    if not e or not e.get('id'):
                        continue
                    vid = e.get('id')
                    raw_ttl = e.get('title') or "Без названия"
                    uploader = e.get('uploader') or ""
                    dur_sec = int(e.get('duration') or 0)

                    sc = score_track_entry(e, target_artist, target_title)
                    if sc <= -500:
                        continue  # Отсекаем по вето (< 30s или > 20 мин)

                    dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}" if dur_sec > 0 else "?:??"
                    artist, title = parse_artist_and_title(raw_ttl, uploader)
                    thumb = e.get('thumbnail') or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

                    scored_entries.append((sc, {
                        "id": vid,
                        "title": target_title if (target_title and sc >= 120) else title,
                        "artist": target_artist if (target_artist and sc >= 120) else artist,
                        "raw_title": raw_ttl,
                        "duration_sec": dur_sec,
                        "duration_str": dur_str,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "thumbnail_url": thumb,
                    }))

                if scored_entries:
                    # Сортируем по убыванию релевантности
                    scored_entries.sort(key=lambda x: x[0], reverse=True)
                    return [item[1] for item in scored_entries[:limit]]

        except Exception as e:
            last_err = e
            logger.warning(f"Track search warning for proxy '{p}': {e}")

    if last_err:
        logger.error(f"Track search error for '{query}': {last_err}")
    return []


async def search_tracks(
    query: str,
    limit: int = 5,
    target_artist: str = "",
    target_title: str = "",
    proxy_url: str = None
) -> List[Dict[str, Any]]:
    """Асинхронный поиск треков по названию с оценкой качества и фильтрацией мемов."""
    return await asyncio.to_thread(_search_youtube_tracks_sync, query, limit, target_artist, target_title, proxy_url)
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
