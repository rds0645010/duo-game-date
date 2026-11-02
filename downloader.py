import os
import re
import time
import shutil
import asyncio
import logging
import aiohttp
import yt_dlp
import imageio_ffmpeg

import warp_proxy

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
INSTAGRAM_REGEX = re.compile(r'https?://(?:www\.)?instagram\.com/(?:reel|p|reels)/[^\s]+')

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
    Fast direct parser for TikTok without watermark via TikWM API.
    Returns: dict(video_url=..., title=..., author=..., cover=..., duration=...)
    """
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(api_url, data={"url": url}, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0 and data.get("data"):
                        d = data["data"]
                        video_url = d.get("play") or d.get("wmplay")
                        if video_url:
                            if video_url.startswith("/"):
                                video_url = "https://www.tikwm.com" + video_url
                            return {
                                "success": True,
                                "platform": "tiktok",
                                "video_url": video_url,
                                "title": d.get("title", "TikTok Video") or "TikTok Video",
                                "author": d.get("author", {}).get("nickname", "TikTok"),
                                "cover": d.get("cover", ""),
                                "duration": d.get("duration", 0)
                            }
                        # Поддержка фото-слайдшоу TikTok
                        images = d.get("images") or []
                        if images:
                            return {
                                "success": True,
                                "platform": "tiktok",
                                "is_photo": True,
                                "photo_urls": images,
                                "title": d.get("title", "TikTok Фото") or "TikTok Фото",
                                "author": d.get("author", {}).get("nickname", "TikTok"),
                                "cover": d.get("cover", "")
                            }
        except Exception as e:
            logger.warning(f"TikWM error for {url}: {e}")
    return None


def is_valid_mp4_file(file_path: str) -> bool:
    """
    Проверяет, что скачанный файл действительно является бинарным медиафайлом (MP4/WebM/MKV),
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
        if b"ftyp" in header or header.startswith(b"\x1a\x45\xdf\xa3"):
            return True
        if header.lstrip().startswith((b"<", b"{", b"[", b"Error", b"HTTP")):
            return False
        return True
    except Exception:
        return False


def extract_youtube_id(url: str):
    m = re.search(
        r'(?:youtu\.be/|youtube\.com/(?:shorts/|live/|watch\?v=|embed/|v/))([A-Za-z0-9_-]{11})',
        url or "",
    )
    return m.group(1) if m else None


async def download_file_from_url(url: str, output_path: str, max_size_bytes: int = 50 * 1024 * 1024):
    """
    Streams a direct video URL to output_path while enforcing max size.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status} while downloading video")
            
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size_bytes:
                raise Exception("file_too_large")
            
            downloaded = 0
            with open(output_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded > max_size_bytes:
                        raise Exception("file_too_large")
                    f.write(chunk)
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
    Быстро извлекает прямой URL видео или фото из публичного Instagram embed (< 1.5 сек).
    Работает без авторизации и cookies.
    """
    m = re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', url)
    if not m:
        return None
    shortcode = m.group(1)
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(embed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    import html as py_html

                    # 1. Извлекаем автора
                    author = ""
                    owner_match = re.search(r'"username":\s*"([^"]+)"', text)
                    if not owner_match:
                        owner_match = re.search(r'class="OwnerUsername[^"]*">([^<]+)<', text)
                    if owner_match:
                        author = py_html.unescape(owner_match.group(1)).strip()

                    # 2. Извлекаем заголовок/описание
                    title = ""
                    cap_match = re.search(r'class="Caption"[^>]*>.*?<div[^>]*>([^<]+)<', text, re.DOTALL)
                    if not cap_match:
                        cap_match = re.search(r'<title>([^<]+)</title>', text)
                    if cap_match:
                        title = py_html.unescape(cap_match.group(1)).strip()

                    # 3. Ищем прямое видео
                    video_matches = re.findall(r'"video_url":\s*"([^"]+)"', text)
                    if video_matches:
                        v_url = py_html.unescape(video_matches[0]).replace("\\/", "/").replace("\\u0026", "&")
                        thumb = ""
                        disp_matches = re.findall(r'"display_url":\s*"([^"]+)"', text)
                        if disp_matches:
                            thumb = py_html.unescape(disp_matches[0]).replace("\\/", "/").replace("\\u0026", "&")
                        return {
                            "success": True,
                            "platform": "instagram",
                            "is_video": True,
                            "video_url": v_url,
                            "title": title or "Instagram Reels",
                            "author": author or "Instagram",
                            "thumbnail": thumb or f"https://www.instagram.com/p/{shortcode}/media/?size=l"
                        }

                    # 4. Ищем прямое фото (если видео нет)
                    photo_matches = re.findall(r'"display_url":\s*"([^"]+)"', text)
                    if not photo_matches:
                        photo_matches = re.findall(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', text)
                    if not photo_matches:
                        photo_matches = re.findall(r'<meta property="og:image" content="([^"]+)"', text)

                    if photo_matches:
                        p_url = py_html.unescape(photo_matches[0]).replace("\\/", "/").replace("\\u0026", "&")
                        return {
                            "success": True,
                            "platform": "instagram",
                            "is_photo": True,
                            "photo_url": p_url,
                            "title": title or "Instagram Фото",
                            "author": author or "Instagram",
                            "thumbnail": p_url
                        }
    except Exception as e:
        logger.debug(f"Instagram embed fast parse failed: {e}")

    # Fallback на официальный публичный медиа-редирект
    media_url = f"https://www.instagram.com/p/{shortcode}/media/?size=l"
    return {
        "success": True,
        "platform": "instagram",
        "is_photo": True,
        "photo_url": media_url,
        "title": "Instagram Фото",
        "author": "Instagram",
        "thumbnail": media_url
    }


async def try_download_instagram_media(shortcode: str, output_prefix: str):
    """
    Скачивает видео, фотопост или карусель (альбом) из Instagram.
    Если обнаружено видео в embed, скачивает его напрямую.
    Если обнаружено несколько слайдов, возвращает список фото для Telegram MediaGroup.
    Если один слайд с аудио, конвертирует в видео.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }

    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    img_urls = []
    audio_url = None
    direct_video_url = None
    title = ""
    author = ""

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(embed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    import html as py_html

                    # Ищем автора
                    owner_match = re.search(r'"username":\s*"([^"]+)"', html_text)
                    if not owner_match:
                        owner_match = re.search(r'class="OwnerUsername[^"]*">([^<]+)<', html_text)
                    if owner_match:
                        author = py_html.unescape(owner_match.group(1)).strip()

                    # Ищем заголовок
                    cap_match = re.search(r'class="Caption"[^>]*>.*?<div[^>]*>([^<]+)<', html_text, re.DOTALL)
                    if not cap_match:
                        cap_match = re.search(r'<title>([^<]+)</title>', html_text)
                    if cap_match:
                        title = py_html.unescape(cap_match.group(1)).strip()

                    # Проверяем, есть ли прямое видео
                    video_matches = re.findall(r'"video_url":\s*"([^"]+)"', html_text)
                    if video_matches:
                        direct_video_url = py_html.unescape(video_matches[0]).replace("\\/", "/").replace("\\u0026", "&")

                    # Ищем display_url из данных json
                    raw_urls = re.findall(r'"display_url":\s*"([^"]+)"', html_text)
                    if not raw_urls:
                        raw_urls = re.findall(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html_text)
                    if not raw_urls:
                        raw_urls = re.findall(r'<meta property="og:image" content="([^"]+)"', html_text)

                    # Ищем аудиодорожку, если есть
                    audio_matches = re.findall(r'"audio_src":\s*"([^"]+)"', html_text)
                    if audio_matches:
                        audio_url = py_html.unescape(audio_matches[0]).replace("\\/", "/").replace("\\u0026", "&")

                    seen = set()
                    for u in raw_urls:
                        cleaned = py_html.unescape(u).replace("\\/", "/").replace("\\u0026", "&")
                        base = cleaned.split("?")[0]
                        if base not in seen:
                            seen.add(base)
                            img_urls.append(cleaned)
        except Exception as e:
            logger.debug(f"Embed parse error: {e}")

    # Если в embed нашлось прямое видео — скачиваем его как видео MP4
    if direct_video_url:
        dest_video = f"{output_prefix}.mp4"
        try:
            await download_file_from_url(direct_video_url, dest_video)
            if os.path.exists(dest_video) and os.path.getsize(dest_video) > 1000:
                return {
                    "file_path": dest_video,
                    "platform": "instagram",
                    "title": title or "Instagram Reels",
                    "author": author or "Instagram",
                    "filesize": os.path.getsize(dest_video)
                }
        except Exception as e:
            logger.warning(f"Direct embed video download failed: {e}")

    # Fallback на прямой media endpoint если ничего не нашлось
    if not img_urls:
        media_url = f"https://www.instagram.com/p/{shortcode}/media/?size=l"
        img_urls = [media_url]

    # Если слайдов больше одного — скачиваем как альбом (карусель)
    if len(img_urls) > 1:
        album_photos = []
        for idx, u in enumerate(img_urls[:10]):
            dest = f"{output_prefix}_{idx}.jpg"
            try:
                await download_file_from_url(u, dest)
                if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                    album_photos.append(dest)
            except Exception as e:
                logger.warning(f"Error downloading album slide {idx}: {e}")
        if len(album_photos) > 1:
            return {
                "is_album": True,
                "photos": album_photos,
                "platform": "instagram",
                "title": title or "Instagram Альбом",
                "author": author or "Instagram"
            }

    # Если слайд один
    single_dest = f"{output_prefix}.jpg"
    try:
        await download_file_from_url(img_urls[0], single_dest)
        # Если есть аудио, конвертируем в видео MP4
        if audio_url and FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
            audio_dest = f"{output_prefix}.mp3"
            mp4_dest = f"{output_prefix}.mp4"
            try:
                await download_file_from_url(audio_url, audio_dest)
                merged = await asyncio.to_thread(_merge_image_audio_sync, single_dest, audio_dest, mp4_dest)
                if merged and os.path.exists(mp4_dest):
                    if os.path.exists(audio_dest): os.remove(audio_dest)
                    if os.path.exists(single_dest): os.remove(single_dest)
                    return {
                        "file_path": mp4_dest,
                        "platform": "instagram",
                        "title": title or "Instagram Видео (Фото со звуком)",
                        "author": author or "Instagram",
                        "filesize": os.path.getsize(mp4_dest)
                    }
            except Exception as e:
                logger.warning(f"Audio merge failed for IG: {e}")

        return {
            "file_path": single_dest,
            "is_photo": True,
            "platform": "instagram",
            "title": title or "Instagram Фото",
            "author": author or "Instagram",
            "filesize": os.path.getsize(single_dest) if os.path.exists(single_dest) else 0
        }
    except Exception as e:
        logger.warning(f"Single photo download failed: {e}")

    return None




def normalize_cookie_line(line: str) -> str:
    """
    Нормализует строку куки в валидный Netscape формат.
    Если при вставке в веб-интерфейс Render знаки табуляции (\\t) превратились
    в пробелы, восстанавливает правильное разделение на 7 колонок.
    """
    line_s = line.strip()
    if not line_s or line_s.startswith("#"):
        return line_s
    parts = line_s.split("\t")
    if len(parts) >= 7:
        return "\t".join(parts[:7])
    # Разбиваем по пробельным символам (максимум 7 частей)
    space_parts = line_s.split(None, 6)
    if len(space_parts) == 7:
        return "\t".join(space_parts)
    return line_s


def setup_cookies_file(platform: str = None):
    """
    Создает файл cookies.txt для yt-dlp ТОЛЬКО ЕСЛИ пользователь передал
    INSTAGRAM_COOKIES, YOUTUBE_COOKIES или YTDLP_COOKIES в переменных окружения.
    """
    cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
    raw_ig = os.environ.get("INSTAGRAM_COOKIES", "").strip()
    raw_yt = os.environ.get("YOUTUBE_COOKIES", "").strip() or os.environ.get("YTDLP_COOKIES", "").strip()

    # Если явных cookies нет — удаляем старый файл cookies.txt, чтобы не использовать капчу
    if not raw_ig and not raw_yt:
        if os.path.exists(cookies_path):
            try:
                os.remove(cookies_path)
            except Exception:
                pass
        return None

    if platform == "youtube" and not raw_yt:
        return None
    if platform == "instagram" and not raw_ig:
        return None

    try:
        with open(cookies_path, "w", encoding="utf-8") as cf:
            cf.write("# Netscape HTTP Cookie File\n")
            if raw_yt:
                cleaned_yt = raw_yt.replace("\\n", "\n")
                if "\t" in cleaned_yt or "\n" in cleaned_yt or " " in cleaned_yt:
                    for line in cleaned_yt.splitlines():
                        norm_line = normalize_cookie_line(line)
                        if norm_line:
                            cf.write(norm_line + "\n")
                else:
                    for part in cleaned_yt.replace("Cookie:", "").split(";"):
                        if "=" in part:
                            k, v = part.strip().split("=", 1)
                            cf.write(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")
            if raw_ig:
                cleaned_ig = raw_ig.replace("\\n", "\n")
                if "\t" in cleaned_ig or "\n" in cleaned_ig or " " in cleaned_ig:
                    for line in cleaned_ig.splitlines():
                        norm_line = normalize_cookie_line(line)
                        if norm_line:
                            cf.write(norm_line + "\n")
                else:
                    for part in cleaned_ig.replace("Cookie:", "").split(";"):
                        if "=" in part:
                            k, v = part.strip().split("=", 1)
                            cf.write(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")
        return cookies_path
    except Exception as ce:
        logger.warning(f"Could not setup cookies file: {ce}")
        return None


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


def _download_ytdlp_sync(
    url: str,
    output_prefix: str,
    height_limit: int = 720,
    max_size_bytes: int = 50 * 1024 * 1024,
    extractor_args: dict = None,
    platform: str = None,
    use_cookies: bool = True,
    proxy_url: str = None,
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

    # Для Telegram лучше H.264 + AAC (mp4), иначе часть клиентов показывает
    # "битое" видео. При наличии FFmpeg сначала берём адаптивные потоки
    # (качество выше), иначе — готовый muxed-поток.
    if has_ffmpeg:
        format_spec = (
            f"bestvideo[height<={height_limit}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height_limit}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height_limit}][vcodec^=avc1]+bestaudio/"
            f"bestvideo[height<={height_limit}]+bestaudio/"
            f"best[height<={height_limit}][ext=mp4]/"
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
        'retries': 2,
        'fragment_retries': 2,
        'socket_timeout': 25,
        'concurrent_fragment_downloads': 1,
        'overwrites': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

    if proxy:
        ydl_opts['proxy'] = proxy

    if has_cookies:
        ydl_opts['cookiefile'] = cookie_file

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
        ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts['postprocessor_args'] = {
            'ffmpeg': ['-movflags', '+faststart']
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Ищем именно результат текущей попытки (prefix чистился выше).
        candidates = [f"{output_prefix}.{ext}" for ext in ("mp4", "mkv", "webm")]
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
            if is_valid_mp4_file(cand):
                return {
                    "file_path": cand,
                    "title": info.get("title", "Video") if info else "Video",
                    "duration": info.get("duration", 0) if info else 0,
                    "author": (
                        (info.get("uploader") or info.get("channel") or "Creator")
                        if info else "Creator"
                    ),
                    "filesize": os.path.getsize(cand),
                }
        raise Exception("Downloaded file not valid or not found on disk")
    except Exception:
        _cleanup_prefix(output_prefix)
        raise


async def _download_youtube(clean_url: str, output_prefix: str):
    """YouTube-ветка: WARP -> прямой IP -> понижение качества."""
    # 2. YOUTUBE SHORTS / Обычные видео:
    # Главный приём: WARP-прокси (usque) подменяет IP Render на Cloudflare,
    # поэтому YouTube не показывает "Sign in to confirm you're not a bot".
    # Никакой регистрации, cookies и аккаунта Google не требуется.
    is_short = "/shorts/" in clean_url
    # У вертикальных Shorts 720p — это 720x1280 (height=1280).
    # Для обычных видео ограничиваемся 720p по высоте.
    heights = [1280, 480, 360] if is_short else [720, 480, 360]

    manual_proxy = (
        os.getenv("YTDLP_PROXY")
        or os.getenv("YOUTUBE_PROXY")
        or os.getenv("PROXY")
        or os.getenv("HTTPS_PROXY")
        or ""
    ).strip()

    strategies = []

    warp_ready = False
    if warp_proxy.warp.enabled and warp_proxy.warp.supported:
        try:
            warp_ready = await warp_proxy.warp.ensure_ready()
        except Exception as e:
            logger.warning(f"WARP init error: {e}")
    if warp_ready and warp_proxy.warp.proxy_url:
        strategies.append(("warp", warp_proxy.warp.proxy_url))

    # Ручной прокси — только запасной вариант после WARP. Если он остался
    # в env с прошлых экспериментов и умер, он не должен задерживать YouTube.
    if manual_proxy:
        strategies.append(("manual-proxy", manual_proxy))

    # Всегда оставляем прямой запасной вариант.
    strategies.append(("direct", ""))

    if not strategies:
        strategies = [("direct", "")]

    last_error = None
    size_error = False
    visionos_args = {'youtube': {'player_client': ['visionos']}}
    attempted = set()

    for height in heights:
        for strategy_name, strategy_proxy in strategies:
            key = (height, strategy_name)
            if key in attempted:
                continue
            attempted.add(key)
            try:
                res = await asyncio.to_thread(
                    _download_ytdlp_sync,
                    clean_url,
                    output_prefix,
                    height,
                    extractor_args=visionos_args,
                    use_cookies=False,
                    proxy_url=strategy_proxy,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[YouTube] {strategy_name} {height}p failed for {clean_url}: {e}"
                )
                # Если завис именно туннель WARP — сбросим его, чтобы
                # следующий запрос поднял прокси заново.
                if strategy_name == "warp" and any(
                    token in str(e).lower()
                    for token in (
                        "proxy", "tunnel", "timed out", "connection", "403",
                        "not a bot", "sign in", "login_required",
                    )
                ):
                    try:
                        await warp_proxy.warp.mark_unhealthy()
                    except Exception:
                        pass
                continue

            if not res or not res.get("file_path"):
                last_error = Exception("empty download result")
                continue

            size = res.get("filesize") or 0
            if size > 50 * 1024 * 1024:
                size_error = True
                logger.warning(
                    f"[YouTube] {strategy_name} {height}p: файл {size / 1048576:.1f} МБ > 50 МБ, пробую ниже"
                )
                try:
                    if os.path.exists(res["file_path"]):
                        os.remove(res["file_path"])
                except OSError:
                    pass
                continue

            res["platform"] = "youtube"
            if height < heights[0]:
                res["low_quality_notice"] = True
            logger.info(
                f"✅ [YouTube] Скачано через {strategy_name} ({height}p, {size / 1048576:.1f} МБ)"
            )
            return res

        # Если на этом качестве всё упало по размеру — сразу пробуем ниже.
        # Если же были сетевые ошибки, тоже пробуем ниже: 360p часто проходит.
        continue

    if size_error and last_error is None:
        raise Exception("file_too_large: видео больше 50 МБ даже в 360p")
    raise Exception(f"youtube_bot_detection: {last_error}")


async def download_video(url: str, user_id: int):
    """
    Главный диспетчер скачивания:
    - TikTok -> быстрый TikWM API (без водяного знака);
    - YouTube Shorts / видео -> WARP-прокси + yt-dlp (visionos client),
      без cookies и регистрации, с fallback на прямой IP и понижение качества;
    - Instagram -> yt-dlp + embed/photo fallback.
    """
    platform = detect_platform(url)
    if not platform:
        raise Exception("unsupported_platform")

    clean_url = extract_url(url) or url
    import uuid
    uid = str(uuid.uuid4())[:8]
    output_prefix = os.path.join(DOWNLOAD_DIR, f"vid_{user_id}_{uid}")

    # 1. TIKTOK: Fast TikWM API
    if platform == "tiktok":
        tikwm_data = await parse_tiktok_tikwm(clean_url)
        if tikwm_data:
            if tikwm_data.get("video_url"):
                dest_file = f"{output_prefix}.mp4"
                try:
                    await download_file_from_url(tikwm_data["video_url"], dest_file)
                    return {
                        "file_path": dest_file,
                        "title": tikwm_data.get("title", "TikTok"),
                        "author": tikwm_data.get("author", "TikTok"),
                        "duration": tikwm_data.get("duration", 0),
                        "platform": "tiktok",
                        "direct_url": tikwm_data["video_url"],
                        "cover": tikwm_data.get("cover", ""),
                        "filesize": os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
                    }
                except Exception as e:
                    logger.warning(f"TikWM stream download failed: {e}, falling back to yt-dlp...")
                    if os.path.exists(dest_file):
                        os.remove(dest_file)
            elif tikwm_data.get("is_photo") and tikwm_data.get("photo_urls"):
                photo_urls = tikwm_data["photo_urls"]
                # Если несколько картинок в слайдшоу TikTok — формируем альбом
                if len(photo_urls) > 1:
                    album_photos = []
                    for idx, img_u in enumerate(photo_urls[:10]):
                        dest = f"{output_prefix}_{idx}.jpg"
                        try:
                            await download_file_from_url(img_u, dest)
                            if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                                album_photos.append(dest)
                        except Exception as dl_err:
                            logger.warning(f"TikTok album slide download error: {dl_err}")
                    if len(album_photos) > 1:
                        return {
                            "is_album": True,
                            "photos": album_photos,
                            "platform": "tiktok",
                            "title": tikwm_data.get("title", "TikTok Слайдшоу"),
                            "author": tikwm_data.get("author", "TikTok")
                        }

                # Если одна картинка — пробуем наложить звук
                dest_file = f"{output_prefix}.jpg"
                try:
                    await download_file_from_url(photo_urls[0], dest_file)
                    music_url = tikwm_data.get("music")
                    if music_url and FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
                        audio_file = f"{output_prefix}.mp3"
                        mp4_file = f"{output_prefix}.mp4"
                        try:
                            await download_file_from_url(music_url, audio_file)
                            merged = await asyncio.to_thread(_merge_image_audio_sync, dest_file, audio_file, mp4_file)
                            if merged and os.path.exists(mp4_file):
                                if os.path.exists(audio_file): os.remove(audio_file)
                                if os.path.exists(dest_file): os.remove(dest_file)
                                return {
                                    "file_path": mp4_file,
                                    "platform": "tiktok",
                                    "title": tikwm_data.get("title", "TikTok Видео (Фото со звуком)"),
                                    "author": tikwm_data.get("author", "TikTok"),
                                    "filesize": os.path.getsize(mp4_file)
                                }
                        except Exception as m_err:
                            logger.warning(f"TikTok music merge error: {m_err}")

                    return {
                        "file_path": dest_file,
                        "is_photo": True,
                        "title": tikwm_data.get("title", "TikTok Фото"),
                        "author": tikwm_data.get("author", "TikTok"),
                        "platform": "tiktok",
                        "filesize": os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
                    }
                except Exception as e:
                    logger.warning(f"TikTok photo download failed: {e}")

    # 2. YOUTUBE SHORTS / Обычные видео:
    # WARP-прокси подменяет IP Render на Cloudflare, чтобы YouTube не показывал
    # "Sign in to confirm you're not a bot". Без регистрации, cookies и аккаунта.
    if platform == "youtube":
        async with _youtube_download_lock:
            return await _download_youtube(clean_url, output_prefix)

    # 3. INSTAGRAM REELS & TIKTOK FALLBACK:
    try:
        res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 720)
        if res and is_valid_mp4_file(res.get("file_path")):
            res["platform"] = platform
            return res
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"Download 720p failed for {clean_url}: {e}")

        # Для Instagram: пробуем embed/альбомный парсер при ЛЮБОЙ ошибке yt-dlp
        if platform == "instagram":
            m = re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', clean_url)
            if m:
                shortcode = m.group(1)
                try:
                    logger.info(f"Attempting Instagram embed/photo fallback for {shortcode}...")
                    media_res = await try_download_instagram_media(shortcode, output_prefix)
                    if media_res:
                        return media_res
                except Exception as fb_err:
                    logger.warning(f"Instagram fallback failed: {fb_err}")

        # Если в публикации нет видео (это фотопост или карусель в Instagram)
        if "no video in this post" in err_msg.lower():
            raise Exception("is_photo_post")

        # If file too large, retry in lower resolution (360p)
        if "max_filesize" in err_msg or "too large" in err_msg.lower():
            try:
                logger.info(f"Retrying in lower resolution (360p) for {clean_url}...")
                res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 360)
                if res and is_valid_mp4_file(res.get("file_path")):
                    res["platform"] = platform
                    res["low_quality_notice"] = True
                    return res
            except Exception as e2:
                raise Exception(f"file_too_large: {e2}")

        # Если это возрастное ограничение (18+) или закрытая аудитория в Instagram
        if "available to everyone" in err_msg.lower() or "certain audiences" in err_msg.lower():
            raise Exception("instagram_age_restricted")

        raise e

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

