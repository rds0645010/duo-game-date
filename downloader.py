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
    # First attempt: 720p
    try:
        res = await asyncio.to_thread(_download_ytdlp_sync, clean_url, output_prefix, 720)
        res["platform"] = platform
        return res
    except Exception as e:
        err_msg = str(e)
        logger.warning(f"Download 720p failed for {clean_url}: {e}")

        # Если в публикации нет видео (это фотопост или карусель в Instagram)
        if "no video in this post" in err_msg.lower():
            m = re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', clean_url)
            if m:
                shortcode = m.group(1)
                media_res = await try_download_instagram_media(shortcode, output_prefix)
                if media_res:
                    return media_res
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

