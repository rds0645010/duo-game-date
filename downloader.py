import os
import re
import asyncio
import logging
import aiohttp
import yt_dlp
import imageio_ffmpeg

logger = logging.getLogger(__name__)

# Temporary directory for video files
DOWNLOAD_DIR = "temp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Path to ffmpeg binary from imageio-ffmpeg
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        import stat
        os.chmod(FFMPEG_PATH, os.stat(FFMPEG_PATH).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
except Exception as e:
    logger.warning(f"FFmpeg init warning: {e}")
    FFMPEG_PATH = None


def ensure_js_runtime():
    """
    Гарантирует наличие JavaScript runtime (Node.js или Deno) для yt-dlp.
    Без JS runtime YouTube блокирует запросы из облачных дата-центров (Render).
    """
    import shutil
    import subprocess
    import platform

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

    # 3. Проверяем локальную папку bin/
    bin_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
    os.makedirs(bin_dir, exist_ok=True)
    local_deno = os.path.join(bin_dir, "deno" if os.name != "nt" else "deno.exe")
    if os.path.exists(local_deno) and os.path.getsize(local_deno) > 1000:
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        logger.info(f"JS Runtime: Local Deno found at {local_deno}")
        return "deno", local_deno

    # 4. Если на Linux (Render) — автоматически скачиваем standalone Deno v2.9.8
    if platform.system() == "Linux":
        logger.info("⚡ Скачивание standalone Deno runtime для обхода защиты YouTube на Render...")
        import urllib.request
        import zipfile
        import io
        deno_url = "https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-unknown-linux-gnu.zip"
        try:
            req = urllib.request.Request(deno_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = resp.read()
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    zf.extract("deno", path=bin_dir)
            import stat
            os.chmod(local_deno, os.stat(local_deno).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRUSR | stat.S_IWUSR)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            logger.info(f"✅ Deno успешно установлен в {local_deno} и добавлен в PATH")
            return "deno", local_deno
        except Exception as dl_err:
            logger.warning(f"Не удалось скачать Deno: {dl_err}")

    return None, None

# Автоматическая инициализация JS runtime при импорте модуля
try:
    ensure_js_runtime()
except Exception as _js_e:
    logger.debug(f"JS runtime init notice: {_js_e}")

# Regex patterns for supported platforms
TIKTOK_REGEX = re.compile(r'https?://(?:www\.|v[mt]\.)?tiktok\.com/[^\s]+')
YOUTUBE_REGEX = re.compile(r'https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[^\s]+')
INSTAGRAM_REGEX = re.compile(r'https?://(?:www\.)?instagram\.com/(?:reel|p|reels)/[^\s]+')

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
            return m.group(0).split('?')[0] + ('?' + m.group(0).split('?')[1] if '?' in m.group(0) else '')
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




def setup_cookies_file():
    """
    Создает файл cookies.txt для yt-dlp из переменных окружения
    INSTAGRAM_COOKIES и YOUTUBE_COOKIES, либо автоматически генерирует
    анонимную гостевую сессию (Guest Session) с YouTube, не требующую аккаунта.
    """
    cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
    raw_ig = os.environ.get("INSTAGRAM_COOKIES", "").strip()
    raw_yt = os.environ.get("YOUTUBE_COOKIES", "").strip()

    if raw_ig or raw_yt:
        try:
            with open(cookies_path, "w", encoding="utf-8") as cf:
                cf.write("# Netscape HTTP Cookie File\n")
                if raw_yt:
                    if "\t" in raw_yt:
                        cf.write(raw_yt.strip() + "\n")
                    else:
                        for part in raw_yt.replace("Cookie:", "").split(";"):
                            if "=" in part:
                                k, v = part.strip().split("=", 1)
                                cf.write(f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")
                if raw_ig:
                    if "\t" in raw_ig:
                        cf.write(raw_ig.strip() + "\n")
                    else:
                        for part in raw_ig.replace("Cookie:", "").split(";"):
                            if "=" in part:
                                k, v = part.strip().split("=", 1)
                                cf.write(f".instagram.com\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}\n")
            return cookies_path
        except Exception as ce:
            logger.warning(f"Could not setup cookies file: {ce}")

    if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 10:
        return cookies_path

    # Автоматическая генерация анонимных гостевых cookies без входа в аккаунт
    import http.cookiejar
    import urllib.request
    try:
        cj = http.cookiejar.MozillaCookieJar(cookies_path)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(
            'https://www.youtube.com',
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        )
        opener.open(req, timeout=4)
        cj.save(ignore_discard=True, ignore_expires=True)
        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 10:
            return cookies_path
    except Exception as ge:
        logger.debug(f"Guest cookie auto-generation skipped: {ge}")

    return None


def _download_ytdlp_sync(url: str, output_prefix: str, height_limit: int = 720, max_size_bytes: int = 50 * 1024 * 1024, extractor_args: dict = None):
    """
    Synchronous yt-dlp execution run inside asyncio.to_thread.
    Uses Apple VisionOS HLS protocol (m3u8_native) to bypass YouTube bot detection and 403 Forbidden
    on datacenter IPs completely anonymously without needing a Google account or cookies.
    """
    out_tmpl = f"{output_prefix}.%(ext)s"
    
    # Priority cascade:
    # 1. HLS m3u8_native video + audio (visionos bypass)
    # 2. H.264 (avc1) + AAC (mp4a) for native fast playback in Telegram
    # 3. Best video + best audio merged into MP4
    # 4. Best single pre-merged MP4 stream
    format_spec = (
        f"bestvideo[protocol^=m3u8][height<={height_limit}]+bestaudio[protocol^=m3u8]/"
        f"bestvideo[height<={height_limit}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height<={height_limit}]+bestaudio/"
        f"best[height<={height_limit}][ext=mp4]/"
        f"best[height<={height_limit}]/"
        f"best"
    )
    
    cookie_file = setup_cookies_file()

    ydl_opts = {
        'outtmpl': out_tmpl,
        'format': format_spec,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': max_size_bytes,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15',
            'Accept-Language': 'en-US,en;q=0.9',
        },
        'extractor_args': extractor_args or {
            'youtube': {
                'player_client': ['visionos', 'android_vr']
            }
        },
        'js_runtimes': {
            'node': {},
            'deno': {},
            'quickjs': {}
        },
        'remote_components': {'ejs:github'},
    }

    if cookie_file and os.path.exists(cookie_file):
        ydl_opts['cookiefile'] = cookie_file

    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        ydl_opts['ffmpeg_location'] = FFMPEG_PATH
        ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]
        ydl_opts['postprocessor_args'] = {
            'ffmpeg': ['-movflags', '+faststart']
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Find created file
        for ext in ['mp4', 'mkv', 'webm']:
            cand = f"{output_prefix}.{ext}"
            if os.path.exists(cand):
                return {
                    "file_path": cand,
                    "title": info.get("title", "Video"),
                    "duration": info.get("duration", 0),
                    "author": info.get("uploader", "") or info.get("channel", "") or "Creator",
                    "filesize": os.path.getsize(cand)
                }
        # Fallback if filename differed
        files = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(os.path.basename(output_prefix))]
        if files:
            p = os.path.join(DOWNLOAD_DIR, files[0])
            return {
                "file_path": p,
                "title": info.get("title", "Video"),
                "duration": info.get("duration", 0),
                "author": info.get("uploader", "") or "Creator",
                "filesize": os.path.getsize(p)
            }
        raise Exception("Downloaded file not found on disk")


async def download_video(url: str, user_id: int):
    """
    Main download dispatcher:
    - If TikTok -> TikWM API direct download first (instant and high quality without watermark).
    - If YouTube Shorts / Instagram -> yt-dlp with automatic 720p fallback to 360p if large.
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

    # 2. YOUTUBE SHORTS / INSTAGRAM REELS / TIKTOK FALLBACK
    # First attempt: 720p (with visionos & android_vr priority for YouTube)
    try:
        yt_args = {'youtube': {'player_client': ['visionos', 'android_vr']}} if platform == "youtube" else None
        res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 720, extractor_args=yt_args)
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

        # Если YouTube: пробуем fallback 480p/360p с visionos/android_vr
        if platform == "youtube":
            try:
                logger.info(f"Retrying YouTube with 480p lower resolution fallback for {clean_url}...")
                yt_fb_args = {'youtube': {'player_client': ['visionos', 'android_vr']}}
                res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 480, extractor_args=yt_fb_args)
                res["platform"] = platform
                res["low_quality_notice"] = True
                return res
            except Exception as yt_err:
                logger.warning(f"YouTube 480p fallback failed: {yt_err}")
                if "sign in to confirm" in str(yt_err).lower() or "confirm you're not a bot" in str(yt_err).lower():
                    raise Exception("youtube_bot_detection")

        # If file too large, retry in lower resolution (360p)
        if "max_filesize" in err_msg or "too large" in err_msg.lower():
            try:
                logger.info(f"Retrying in lower resolution (360p) for {clean_url}...")
                yt_args = {'youtube': {'player_client': ['visionos', 'android_vr']}} if platform == "youtube" else None
                res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 360, extractor_args=yt_args)
                res["platform"] = platform
                res["low_quality_notice"] = True
                return res
            except Exception as e2:
                raise Exception(f"file_too_large: {e2}")

        # Если это возрастное ограничение (18+) или закрытая аудитория в Instagram
        if "available to everyone" in err_msg.lower() or "certain audiences" in err_msg.lower():
            raise Exception("instagram_age_restricted")

        raise e


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
        cookie_file = setup_cookies_file()
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
                    'player_client': ['tv_samsung', 'visionos', 'web', 'android_vr']
                }
            },
            'js_runtimes': {
                'node': {},
                'deno': {},
                'quickjs': {}
            },
            'remote_components': {'ejs:github'},
        }
        if cookie_file and os.path.exists(cookie_file):
            ydl_opts['cookiefile'] = cookie_file

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

