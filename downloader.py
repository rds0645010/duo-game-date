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


async def try_download_instagram_photo(shortcode: str, output_path: str):
    """
    Скачивает изображение в высоком разрешении для фотопостов Instagram.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    }
    # 1. Прямой редирект медиа
    media_url = f"https://www.instagram.com/p/{shortcode}/media/?size=l"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(media_url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
                    content = await resp.read()
                    if len(content) > 1000:
                        with open(output_path, "wb") as f:
                            f.write(content)
                        return output_path
        except Exception as e:
            logger.debug(f"Direct media photo failed: {e}")

    # 2. Парсинг превью из embed страницы
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(embed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    img_matches = re.findall(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html_text)
                    if not img_matches:
                        img_matches = re.findall(r'<meta property="og:image" content="([^"]+)"', html_text)
                    if img_matches:
                        import html as py_html
                        img_url = py_html.unescape(img_matches[0])
                        async with session.get(img_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as img_resp:
                            if img_resp.status == 200:
                                content = await img_resp.read()
                                with open(output_path, "wb") as f:
                                    f.write(content)
                                return output_path
        except Exception as e:
            logger.debug(f"Embed photo parse failed: {e}")

    return None



def _download_ytdlp_sync(url: str, output_prefix: str, height_limit: int = 720, max_size_bytes: int = 50 * 1024 * 1024):
    """
    Synchronous yt-dlp execution run inside asyncio.to_thread.
    """
    out_tmpl = f"{output_prefix}.%(ext)s"
    
    # Try pre-merged MP4 first (works even without ffmpeg), then bestvideo+bestaudio
    format_spec = f"best[ext=mp4][height<={height_limit}]/bestvideo[height<={height_limit}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height_limit}][ext=mp4]/best[height<={height_limit}]/best"
    
    ydl_opts = {
        'outtmpl': out_tmpl,
        'format': format_spec,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': max_size_bytes,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }

    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        ydl_opts['ffmpeg_location'] = FFMPEG_PATH
        ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]

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
                dest_file = f"{output_prefix}.jpg"
                try:
                    await download_file_from_url(tikwm_data["photo_urls"][0], dest_file)
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
    # First attempt: 720p
    try:
        res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 720)
        res["platform"] = platform
        return res
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"Download 720p failed for {clean_url}: {e}")

        # Если в публикации нет видео (это фотопост в Instagram)
        if "no video in this post" in err_msg.lower():
            m = re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', clean_url)
            if m:
                shortcode = m.group(1)
                photo_file = f"{output_prefix}.jpg"
                photo_res = await try_download_instagram_photo(shortcode, photo_file)
                if photo_res and os.path.exists(photo_res):
                    return {
                        "file_path": photo_res,
                        "is_photo": True,
                        "platform": "instagram",
                        "title": "Instagram Фото",
                        "author": "",
                        "filesize": os.path.getsize(photo_res)
                    }
            raise Exception("is_photo_post")

        # If file too large or format error, retry in lower resolution (360p)
        if "max_filesize" in err_msg or "too large" in err_msg.lower():
            try:
                logger.info(f"Retrying in lower resolution (360p) for {clean_url}...")
                res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 360)
                res["platform"] = platform
                res["low_quality_notice"] = True
                return res
            except Exception as e2:
                raise Exception(f"file_too_large: {e2}")
        raise e


async def extract_direct_video_info(url: str):
    """
    Fast metadata extractor for Telegram Inline Query (< 2.5 seconds timeout).
    Returns direct MP4 streaming URL so Telegram can send the actual video without downloading locally.
    """
    platform = detect_platform(url)
    clean_url = extract_url(url) or url

    # 1. TikTok: instant via TikWM (< 0.5s)
    if platform == "tiktok":
        tikwm_data = await parse_tiktok_tikwm(clean_url)
        if tikwm_data and tikwm_data.get("video_url"):
            return {
                "success": True,
                "platform": "tiktok",
                "video_url": tikwm_data["video_url"],
                "title": tikwm_data.get("title") or "TikTok",
                "author": tikwm_data.get("author") or "TikTok",
                "thumbnail": tikwm_data.get("cover") or ""
            }

    # 2. Instagram & other direct streams via fast yt-dlp metadata extract
    def _fast_extract():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
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

