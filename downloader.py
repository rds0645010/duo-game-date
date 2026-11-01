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
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

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


def _download_ytdlp_sync(url: str, output_prefix: str, height_limit: int = 720, max_size_bytes: int = 50 * 1024 * 1024):
    """
    Synchronous yt-dlp execution run inside asyncio.to_thread.
    """
    out_tmpl = f"{output_prefix}.%(ext)s"
    
    # Try preferred format (up to height_limit, merged to mp4)
    format_spec = f"bestvideo[height<={height_limit}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height_limit}][ext=mp4]/best[height<={height_limit}]/best"
    
    ydl_opts = {
        'outtmpl': out_tmpl,
        'format': format_spec,
        'ffmpeg_location': FFMPEG_PATH,
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'max_filesize': max_size_bytes,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
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
        if tikwm_data and tikwm_data.get("video_url"):
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
                    "cover": tikwm_data.get("cover", "")
                }
            except Exception as e:
                logger.warning(f"TikWM stream download failed: {e}, falling back to yt-dlp...")
                if os.path.exists(dest_file):
                    os.remove(dest_file)

    # 2. YOUTUBE SHORTS / INSTAGRAM REELS / TIKTOK FALLBACK
    # First attempt: 720p
    try:
        res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 720)
        res["platform"] = platform
        return res
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"Download 720p failed for {clean_url}: {e}")
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
