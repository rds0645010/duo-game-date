import os
import re
import time
import shutil
import asyncio
import logging
import aiohttp
import yt_dlp
import imageio_ffmpeg
from typing import Optional, Dict, Any, List

import warp_proxy
import tiktok_service
import youtube_service
import instagram_service

logger = logging.getLogger(__name__)

# Temporary directory for video files
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Путь к FFmpeg. imageio-ffmpeg несёт собранный бинарник, поэтому на Render
# ничего дополнительно устанавливать не нужно.
FFMPEG_PATH = None
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        try:
            import stat
            os.chmod(
                FFMPEG_PATH,
                os.stat(FFMPEG_PATH).st_mode
                | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH,
            )
        except OSError:
            # На Windows/read-only ФС chmod не нужен: .exe и так исполняемый.
            pass
except Exception as e:
    logger.warning(f"FFmpeg init warning: {e}")

if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH):
    FFMPEG_PATH = shutil.which("ffmpeg")

if not FFMPEG_PATH:
    logger.warning("FFmpeg не найден: склейка видео+звук для YouTube будет недоступна")


def ensure_js_runtime():
    """
    Гарантирует наличие JavaScript runtime (Node.js или Deno) для yt-dlp.
    Без JS runtime YouTube блокирует запросы из облачных дата-центров (Render).
    """
    import shutil
    import subprocess

    # 1. Проверяем node в PATH
    node_path = shutil.which("node")
    if node_path:
        try:
            out = subprocess.check_output([node_path, "--version"], text=True, timeout=3).strip()
            logger.info(f"JS Runtime: Node.js detected at {node_path} ({out})")
            return "node", node_path
        except Exception as e:
            logger.debug(f"Node.js check error: {e}")

    # 2. Проверяем deno в PATH
    deno_path = shutil.which("deno")
    if deno_path:
        try:
            out = subprocess.check_output([deno_path, "--version"], text=True, timeout=3).strip().splitlines()[0]
            logger.info(f"JS Runtime: Deno detected at {deno_path} ({out})")
            return "deno", deno_path
        except Exception as e:
            logger.debug(f"Deno check error: {e}")

    # 3. Проверяем стандартные пути установки Deno на Linux/Render (~/.deno/bin)
    candidate_deno_paths = [
        os.path.expanduser("~/.deno/bin/deno"),
        "/opt/render/.deno/bin/deno",
        "/root/.deno/bin/deno",
        os.path.join(os.getcwd(), "bin", "deno" if os.name != "nt" else "deno.exe")
    ]
    for cand in candidate_deno_paths:
        if os.path.exists(cand) and os.path.getsize(cand) > 1000:
            cand_dir = os.path.dirname(cand)
            if cand_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = cand_dir + os.pathsep + os.environ.get("PATH", "")
            logger.info(f"JS Runtime: Found Deno at {cand} and added to PATH")
            return "deno", cand

    # 4. Проверяем локальную папку bin/
    bin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    os.makedirs(bin_dir, exist_ok=True)
    local_deno = os.path.join(bin_dir, "deno" if os.name != "nt" else "deno.exe")
    if os.path.exists(local_deno) and os.path.getsize(local_deno) > 1000:
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        logger.info(f"JS Runtime: Local Deno found at {local_deno}")
        return "deno", local_deno

    # Автоскачивание Deno при старте отключено: usque/WARP + visionos-клиент
    # работают без JS runtime, а лишние 40 МБ на диске и 10-20 сек cold start
    # на бесплатном Render ни к чему. Runtime ставится на этапе сборки.
    return None, None

# Автоматическая инициализация JS runtime при импорте модуля
try:
    ensure_js_runtime()
except Exception as _js_e:
    logger.debug(f"JS runtime init notice: {_js_e}")

# Regex patterns for supported platforms
TIKTOK_REGEX = re.compile(r'https?://(?:www\.|v[mt]\.)?tiktok\.com/[^\s]+')
YOUTUBE_REGEX = re.compile(
    r'https?://(?:www\.|m\.|music\.)?'
    r'(?:youtube\.com/(?:watch\?[^\s#]*v=|shorts/|live/|embed/|v/)|youtu\.be/)[^\s]+'
)
INSTAGRAM_REGEX = re.compile(r'https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv|share/(?:reel|p))/[^\s]+')

# Хвостовая пунктуация из сообщений: "вот ссылка: https://youtu.be/xxx."
_URL_TRAILING = '.,;:!?)]}>"\''

def detect_platform(text: str):
    if not text:
        return None
    if TIKTOK_REGEX.search(text):
        return "tiktok"
    if YOUTUBE_REGEX.search(text):
        return "youtube"
    if INSTAGRAM_REGEX.search(text):
        return "instagram"
    return None

def extract_url(text: str):
    if not text:
        return None
    for pattern in [TIKTOK_REGEX, YOUTUBE_REGEX, INSTAGRAM_REGEX]:
        m = pattern.search(text)
        if m:
            return m.group(0).rstrip(_URL_TRAILING)
    return None


async def parse_tiktok_tikwm(url: str):
    """
    Быстрый парсер TikTok без водяных знаков (делегирует в независимый tiktok_service).
    """
    return await tiktok_service.parse_tiktok(url)


def is_valid_mp4_file(file_path: str, allow_audio: bool = True) -> bool:
    """
    Проверяет, что скачанный файл действительно является бинарным медиафайлом (MP4/WebM/MKV/M4A/MP3),
    а не HTML/JSON страницей с ошибкой 403/502 от прокси.
    Исключает появление плашки 'Unknown Track' в Telegram.
    """
    if not file_path or not os.path.exists(file_path):
        return False
    size = os.path.getsize(file_path)
    if size < 16 * 1024:  # Меньше 16 КБ — гарантированно не видео
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(32)
        # MP4/MOV/M4A: ftyp в первых байтах; WebM/MKV: EBML.
        if b"ftyp" in header[:16] or header.startswith(b"\x1a\x45\xdf\xa3"):
            return True
        if allow_audio and (
            header.startswith(b"ID3")
            or header.startswith(b"\xff\xfb")
            or header.startswith(b"\xff\xf3")
            or header.startswith(b"\xff\xf2")
            or header.startswith(b"OggS")
        ):
            return True
        # Картинки (в т.ч. обложки Instagram) — не видео.
        if header.startswith((
            b"\xff\xd8\xff",          # JPEG
            b"\x89PNG\r\n\x1a\n",     # PNG
            b"GIF87a", b"GIF89a",     # GIF
            b"RIFF",                  # AVI/WAV
        )):
            return False
        if header.lstrip().startswith((b"<", b"{", b"[", b"Error", b"HTTP")):
            return False
        # Неизвестный формат не считаем видео: лучше перекачать, чем отправить мусор.
        return False
    except Exception:
        return False


def extract_youtube_id(url: str):
    m = re.search(
        r'(?:youtu\.be/|youtube\.com/(?:shorts/|live/|watch\?v=|embed/|v/))([A-Za-z0-9_-]{11})',
        url or "",
    )
    return m.group(1) if m else None


async def download_file_from_url(
    url: str,
    output_path: str,
    max_size_bytes: int = 50 * 1024 * 1024,
    proxy: str = None,
    progress_callback = None
):
    """
    Streams a direct video URL to output_path while enforcing max size and updating progress.
    Для Instagram CDN обязателен Referer, иначе отдаёт 403/HTML вместо MP4.
    proxy — необязательный HTTP-прокси (например, локальный WARP).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if "instagram" in host or "cdninstagram" in host or "fbcdn" in host:
        headers["Referer"] = "https://www.instagram.com/"
        headers["Origin"] = "https://www.instagram.com"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status} while downloading video")

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/" in content_type or "json" in content_type or "xml" in content_type:
                # Instagram часто отдаёт HTML-заглушку вместо видео при 403.
                raise Exception(f"Expected media, got {content_type or 'unknown content type'}")

            content_length = resp.headers.get("Content-Length")
            total = int(content_length) if content_length and content_length.isdigit() else 0
            if total > max_size_bytes:
                raise Exception("file_too_large")

            downloaded = 0
            t0 = time.time()
            with open(output_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > max_size_bytes:
                        raise Exception("file_too_large")
                    f.write(chunk)
                    if progress_callback:
                        elapsed = time.time() - t0
                        spd = downloaded / elapsed if elapsed > 0.05 else 0
                        eta = (total - downloaded) / spd if (spd > 0 and total > downloaded) else 0
                        try:
                            cb_res = progress_callback(downloaded, total, spd, eta)
                            if asyncio.iscoroutine(cb_res):
                                await cb_res
                        except Exception:
                            pass
    return output_path


def _merge_image_audio_sync(image_path: str, audio_path: str, output_mp4: str) -> bool:
    """
    Конвертирует статичное фото + аудиодорожку в MP4 видео через FFmpeg.
    """
    if not FFMPEG_PATH or not os.path.exists(FFMPEG_PATH):
        return False
    import subprocess
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        "-movflags", "+faststart",
        output_mp4
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
        return proc.returncode == 0 and os.path.exists(output_mp4) and os.path.getsize(output_mp4) > 1000
    except Exception as e:
        logger.warning(f"FFmpeg image+audio merge error: {e}")
        return False


async def parse_instagram_embed(url: str):
    """
    Быстро извлекает прямой URL видео или фото из публичного Instagram embed (делегирует в instagram_service).
    """
    return await instagram_service.parse_instagram_embed(url)


async def try_download_instagram_media(shortcode: str, output_prefix: str, allow_photo: bool = True, proxy_url: str = None):
    return await instagram_service._try_download_instagram_embed_media(
        shortcode,
        output_prefix,
        allow_photo,
        proxy_url,
        download_file_from_url,
        is_valid_mp4_file,
        ffmpeg_path=FFMPEG_PATH
    )


def _cleanup_prefix(output_prefix: str):
    """Удаляет все временные файлы, начинающиеся с output_prefix."""
    try:
        directory = os.path.dirname(output_prefix) or "."
        base = os.path.basename(output_prefix)
        for name in os.listdir(directory):
            if name.startswith(base):
                path = os.path.join(directory, name)
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
    except OSError:
        pass


_stale_cleanup_done = False

# На 512 МБ нельзя допускать два одновременных YouTube-скачивания с FFmpeg.
# TikTok/Instagram этот lock не трогают: они почти не используют RAM.
_youtube_download_lock = asyncio.Lock()


def _cleanup_stale_downloads(max_age_seconds: int = 2 * 60 * 60):
    """Раз в жизни процесса чистит temp_downloads от старых недокачанных файлов."""
    global _stale_cleanup_done
    if _stale_cleanup_done:
        return
    _stale_cleanup_done = True
    try:
        now = time.time()
        for name in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def setup_cookies_file(platform: str = None) -> Optional[str]:
    """
    Создает или находит cookies.txt для yt-dlp.
    Поддерживает как Netscape-формат, так и строку cookies из браузера (sessionid=...; ds_user_id=...).
    """
    raw_ig = os.environ.get("INSTAGRAM_COOKIES", "").strip()
    raw_yt = (
        os.environ.get("YOUTUBE_COOKIES", "").strip()
        or os.environ.get("YTDLP_COOKIES", "").strip()
    )

    cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")

    if platform == "youtube" and not raw_yt:
        return None
    if platform == "instagram" and not raw_ig:
        return None

    raw = raw_yt if platform == "youtube" else raw_ig
    if not raw:
        return None

    try:
        # Если это уже существующий путь к файлу
        if os.path.exists(raw):
            return raw

        # Если это Netscape-формат
        if "# Netscape" in raw or "\t" in raw:
            content = raw
        else:
            # Преобразуем строку вида "sessionid=xyz; ds_user_id=123" в Netscape cookies
            domain = ".youtube.com" if platform == "youtube" else ".instagram.com"
            lines = ["# Netscape HTTP Cookie File", f"# Generated for {domain}"]
            for part in raw.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                k, v = part.split("=", 1)
                lines.append(f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{k.strip()}\t{v.strip()}")
            content = "\n".join(lines) + "\n"

        with open(cookies_path, "w", encoding="utf-8") as cf:
            cf.write(content)
        return cookies_path
    except Exception as e:
        logger.warning(f"Error setting up cookies file: {e}")
        return None


def _download_ytdlp_sync(
    url: str,
    output_prefix: str,
    height_limit: int = 720,
    max_size_bytes: int = 50 * 1024 * 1024,
    extractor_args: dict = None,
    platform: str = None,
    use_cookies: bool = True,
    proxy_url: str = None,
    socket_timeout: float = 25,
    progress_callback = None,
    event_loop = None,
    audio_only: bool = False,
):
    """
    Синхронное скачивание через yt-dlp (запускается в asyncio.to_thread).

    proxy_url:
      * None — взять прокси из env для YouTube (совместимость со старым поведением);
      * ""   — принудительно без прокси;
      * "http://..." — явный прокси (WARP).
    """
    _cleanup_stale_downloads()
    _cleanup_prefix(output_prefix)
    out_tmpl = f"{output_prefix}.%(ext)s"

    plat = platform or detect_platform(url)
    cookie_file = setup_cookies_file(plat) if use_cookies else None
    has_cookies = bool(cookie_file and os.path.exists(cookie_file))

    if proxy_url is None and plat == "youtube":
        proxy = (
            os.getenv("YTDLP_PROXY")
            or os.getenv("YOUTUBE_PROXY")
            or os.getenv("PROXY")
            or os.getenv("HTTPS_PROXY")
        )
    else:
        proxy = proxy_url

    has_ffmpeg = bool(FFMPEG_PATH and os.path.exists(FFMPEG_PATH))

    # Для аудио-режима (длинные YouTube видео/подкасты): скачиваем чистую аудиодорожку
    if audio_only:
        format_spec = (
            "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best"
        )
    elif plat == "instagram":
        format_spec = (
            f"best[height<={height_limit}][ext=mp4]/"
            f"best[ext=mp4]/"
            f"best"
        )
        if socket_timeout == 25:
            socket_timeout = 10
    elif has_ffmpeg:
        format_spec = (
            f"bestvideo[height<={height_limit}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height_limit}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height_limit}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={height_limit}][ext=mp4]/"
            f"bestvideo[height<={height_limit}]+bestaudio/"
            f"best[height<={height_limit}]/"
            f"best"
        )
    else:
        format_spec = (
            f"best[height<={height_limit}][ext=mp4]/"
            f"best[height<={height_limit}]/"
            f"best"
        )

    ydl_opts = {
        'outtmpl': out_tmpl,
        'format': format_spec,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'max_filesize': max_size_bytes,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'retries': 0 if (plat == "instagram" and not proxy) else 2,
        'fragment_retries': 2,
        'socket_timeout': socket_timeout,
        'concurrent_fragment_downloads': 8,
        'buffersize': 1024 * 1024,
        'http_chunk_size': 10485760,
        'overwrites': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

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

    if proxy:
        ydl_opts['proxy'] = proxy

    if has_cookies:
        ydl_opts['cookiefile'] = cookie_file

    if plat == "instagram":
        # Instagram отдаёт логин-стену обычному TLS-клиенту. curl_cffi
        # имитирует браузерный fingerprint, и yt-dlp проходит Polaris-запрос.
        try:
            import curl_cffi  # noqa: F401
            from yt_dlp.networking.impersonate import ImpersonateTarget
            ydl_opts['impersonate'] = ImpersonateTarget('chrome')
        except Exception as imp_err:
            logger.warning(
                "curl_cffi impersonation недоступна: Instagram может вернуть login wall (%s)",
                imp_err,
            )

    if extractor_args:
        ydl_opts['extractor_args'] = extractor_args
    elif plat == "youtube" and not has_cookies:
        # visionos не требует PO-token и не попадает под SABR-ограничения.
        ydl_opts['extractor_args'] = {'youtube': {'player_client': ['visionos']}}

    # JS runtime подключаем только если он реально установлен: для visionos
    # он не обязателен, а лишний запуск Deno/Node на 512 МБ не нужен.
    js_runtimes = {}
    if shutil.which("deno"):
        js_runtimes['deno'] = {}
    if shutil.which("node"):
        js_runtimes['node'] = {}
    if js_runtimes:
        ydl_opts['js_runtimes'] = js_runtimes

    if has_ffmpeg:
        ydl_opts['ffmpeg_location'] = FFMPEG_PATH
        if not audio_only:
            ydl_opts['merge_output_format'] = 'mp4'
            ydl_opts['postprocessor_args'] = {
                'ffmpeg': ['-movflags', '+faststart']
            }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Ищем именно результат текущей попытки (prefix чистился выше).
        candidates = [
            f"{output_prefix}.{ext}"
            for ext in ("mp4", "mkv", "webm", "m4a", "mp3", "opus", "aac")
        ]
        try:
            prefix_files = [
                os.path.join(DOWNLOAD_DIR, name)
                for name in os.listdir(DOWNLOAD_DIR)
                if name.startswith(os.path.basename(output_prefix))
            ]
            prefix_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError:
            prefix_files = []
        candidates.extend(prefix_files)

        for cand in candidates:
            if is_valid_mp4_file(cand, allow_audio=audio_only):
                return {
                    "file_path": cand,
                    "title": info.get("title", "Audio" if audio_only else "Video") if info else "Media",
                    "duration": info.get("duration", 0) if info else 0,
                    "author": (
                        (info.get("uploader") or info.get("channel") or "Creator")
                        if info else "Creator"
                    ),
                    "filesize": os.path.getsize(cand),
                    "is_audio": audio_only,
                }
        raise Exception("Downloaded file not valid or not found on disk")
    except Exception:
        _cleanup_prefix(output_prefix)
        raise


async def _download_youtube(clean_url: str, output_prefix: str, progress_callback=None):
    return await youtube_service.download_youtube(
        clean_url,
        output_prefix,
        _download_ytdlp_sync,
        warp_proxy,
        progress_callback=progress_callback
    )


async def _download_instagram(clean_url: str, output_prefix: str, progress_callback=None):
    return await instagram_service.download_instagram(
        clean_url,
        output_prefix,
        _download_ytdlp_sync,
        warp_proxy,
        download_file_from_url,
        is_valid_mp4_file,
        ffmpeg_path=FFMPEG_PATH,
        progress_callback=progress_callback
    )


async def download_video(url: str, user_id: int, progress_callback=None):
    """
    Главный диспетчер скачивания:
    - TikTok -> быстрый TikWM API (без водяного знака) + live progress;
    - YouTube Shorts / видео / подкасты -> WARP-прокси + yt-dlp (visionos client),
      с авто-переходом в MP3/M4A аудио при превышении 50 МБ (для 1-2 часовых роликов);
    - Instagram -> yt-dlp + embed/photo fallback.
    """
    platform = detect_platform(url)
    if not platform:
        raise Exception("unsupported_platform")

    clean_url = extract_url(url) or url
    import uuid
    uid = str(uuid.uuid4())[:8]
    output_prefix = os.path.join(DOWNLOAD_DIR, f"vid_{user_id}_{uid}")

    # 1. TIKTOK: полностью независимая система
    if platform == "tiktok":
        return await tiktok_service.download_tiktok(
            clean_url,
            output_prefix,
            download_locally=True,
            ffmpeg_path=FFMPEG_PATH,
            progress_callback=progress_callback
        )

    # 2. YOUTUBE SHORTS / Обычные видео / Подкасты: полностью независимая система
    if platform == "youtube":
        return await youtube_service.download_youtube(
            clean_url,
            output_prefix,
            _download_ytdlp_sync,
            warp_proxy,
            progress_callback=progress_callback
        )

    # 3. INSTAGRAM REELS: полностью независимая система
    if platform == "instagram":
        return await instagram_service.download_instagram(
            clean_url,
            output_prefix,
            _download_ytdlp_sync,
            warp_proxy,
            download_file_from_url,
            is_valid_mp4_file,
            ffmpeg_path=FFMPEG_PATH,
            progress_callback=progress_callback
        )

    raise Exception("download_failed")


async def extract_direct_video_info(url: str):
    """
    Fast metadata extractor for Telegram Inline Query (< 2.5 seconds timeout).
    Returns direct MP4 streaming URL or direct JPG photo URL so Telegram can send
    the actual video or photo without downloading locally.
    """
    platform = detect_platform(url)
    clean_url = extract_url(url) or url

    # 1. TikTok: instant via TikWM (< 0.5s)
    if platform == "tiktok":
        tikwm_data = await parse_tiktok_tikwm(clean_url)
        if tikwm_data:
            if tikwm_data.get("video_url"):
                return {
                    "success": True,
                    "platform": "tiktok",
                    "is_video": True,
                    "video_url": tikwm_data["video_url"],
                    "title": tikwm_data.get("title") or "TikTok",
                    "author": tikwm_data.get("author") or "TikTok",
                    "thumbnail": tikwm_data.get("cover") or ""
                }
            elif tikwm_data.get("is_photo") and tikwm_data.get("photo_urls"):
                return {
                    "success": True,
                    "platform": "tiktok",
                    "is_photo": True,
                    "photo_url": tikwm_data["photo_urls"][0],
                    "title": tikwm_data.get("title") or "TikTok Фото",
                    "author": tikwm_data.get("author") or "TikTok",
                    "thumbnail": tikwm_data.get("cover") or tikwm_data["photo_urls"][0]
                }

    # 2. Instagram: instant via public embed (< 1.2s)
    if platform == "instagram":
        ig_data = await parse_instagram_embed(clean_url)
        if ig_data:
            return ig_data

    # 3. YouTube Shorts & other direct streams via fast yt-dlp metadata extract
    def _fast_extract():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'extractor_args': {
                'youtube': {
                    'player_client': ['visionos']
                }
            },
            'js_runtimes': {
                'deno': {},
                'node': {},
            },
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
            v_url = info.get("url")
            if not v_url and info.get("formats"):
                candidates = [
                    f for f in info["formats"]
                    if f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url")
                ]
                if candidates:
                    v_url = candidates[-1].get("url")

            if v_url:
                return {
                    "success": True,
                    "platform": platform or "video",
                    "is_video": True,
                    "video_url": v_url,
                    "title": info.get("title") or "Видео",
                    "author": info.get("uploader") or info.get("channel") or "",
                    "thumbnail": info.get("thumbnail") or ""
                }
            return None

    try:
        res = await asyncio.wait_for(asyncio.to_thread(_fast_extract), timeout=2.4)
        if res:
            return res
    except Exception as e:
        logger.warning(f"Fast extract error for {clean_url}: {e}")

    return None

