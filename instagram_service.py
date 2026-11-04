import os
import re
import asyncio
import logging
import aiohttp
from typing import Optional, Dict, Any, List

logger = logging.getLogger("instagram_service")

# Regex для Instagram (Reels, Posts, Stories/Share, TV)
INSTAGRAM_REGEX = re.compile(
    r'https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv|share/(?:reel|p))/[^\s]+'
)

INSTAGRAM_HEIGHTS = [1280, 720, 480]

EMBED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
}


def is_reel_url(url: str) -> bool:
    return bool(re.search(r'(?:instagram\.com|instagr\.am)/(?:reel|reels|tv|share/reel)/', url or "", re.IGNORECASE))


def extract_shortcode(url: str) -> Optional[str]:
    m = re.search(r'(?:instagram\.com|instagr\.am)/(?:reel|p|reels|tv|share/(?:reel|p))/([A-Za-z0-9_-]+)', url or "")
    return m.group(1) if m else None


async def parse_instagram_embed(url: str) -> Optional[Dict[str, Any]]:
    """
    Быстро извлекает прямой URL видео или фото из публичного Instagram embed (< 1.5 сек).
    Работает без авторизации и cookies.
    """
    shortcode = extract_shortcode(url)
    if not shortcode:
        return None
    is_reel = is_reel_url(url)
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(embed_url, headers=EMBED_HEADERS, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    import html as py_html

                    # Автор
                    author = ""
                    owner_match = re.search(r'"username":\s*"([^"]+)"', text)
                    if not owner_match:
                        owner_match = re.search(r'class="OwnerUsername[^"]*">([^<]+)<', text)
                    if owner_match:
                        author = py_html.unescape(owner_match.group(1)).strip()

                    # Заголовок
                    title = ""
                    cap_match = re.search(r'class="Caption"[^>]*>.*?<div[^>]*>([^<]+)<', text, re.DOTALL)
                    if not cap_match:
                        cap_match = re.search(r'<title>([^<]+)</title>', text)
                    if cap_match:
                        title = py_html.unescape(cap_match.group(1)).strip()

                    # Прямое видео
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

                    # Прямое фото (только для обычных фотопостов, не для Reels)
                    photo_matches = re.findall(r'"display_url":\s*"([^"]+)"', text)
                    if not photo_matches:
                        photo_matches = re.findall(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', text)
                    if not photo_matches:
                        photo_matches = re.findall(r'<meta property="og:image" content="([^"]+)"', text)

                    if photo_matches and not is_reel:
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

    # Для Reels фото-фолбэк строго запрещен: это видео, а не фото
    if is_reel:
        return None

    # Для обычных фотопостов /p/
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


async def download_instagram(
    clean_url: str,
    output_prefix: Optional[str] = None,
    ytdlp_sync_func=None,
    warp_proxy_module=None,
    download_file_func=None,
    is_valid_mp4_func=None,
    ffmpeg_path: str = None
) -> Dict[str, Any]:
    """
    Полностью независимый конвейер загрузки Instagram:
    - yt-dlp с curl_cffi-имперсонацией (Chrome TLS fingerprint)
    - Локальный WARP прокси для анонимного запроса (logged-out Polaris)
    - allow_photo=False для Reels (никогда не подменяет видео обложкой)
    - Embed-fallback с поддержкой каруселей и фото для обычных постов
    """
    if not output_prefix:
        import tempfile, uuid
        output_prefix = os.path.join(tempfile.gettempdir(), f"ig_{uuid.uuid4().hex[:8]}")
    is_reel = is_reel_url(clean_url)

    strategies = []
    if warp_proxy_module and warp_proxy_module.warp.enabled and warp_proxy_module.warp.supported:
        try:
            if await warp_proxy_module.warp.ensure_ready() and warp_proxy_module.warp.proxy_url:
                strategies.append(("warp", warp_proxy_module.warp.proxy_url))
        except Exception as e:
            logger.warning(f"Instagram WARP init error: {e}")
    strategies.append(("direct", ""))

    last_error = None
    size_error = False
    no_video = False

    for height in INSTAGRAM_HEIGHTS:
        for strategy_name, strategy_proxy in strategies:
            try:
                extra = {"socket_timeout": 8} if strategy_name == "direct" else {}
                res = await asyncio.to_thread(
                    ytdlp_sync_func,
                    clean_url,
                    output_prefix,
                    height,
                    platform="instagram",
                    use_cookies=(strategy_name == "direct"),
                    proxy_url=strategy_proxy,
                    **extra,
                )
            except Exception as e:
                last_error = e
                logger.warning(f"[Instagram] {strategy_name} {height}p failed: {e}")
                if "no video in this post" in str(e).lower():
                    no_video = True
                    break
                continue

            if not res or not res.get("file_path"):
                last_error = Exception("empty download result")
                continue

            size = res.get("filesize") or 0
            if size > 50 * 1024 * 1024:
                size_error = True
                logger.warning(f"[Instagram] {strategy_name} {height}p: {size / 1048576:.1f} МБ > 50 МБ")
                try:
                    if os.path.exists(res["file_path"]):
                        os.remove(res["file_path"])
                except OSError:
                    pass
                continue

            res["platform"] = "instagram"
            if height < INSTAGRAM_HEIGHTS[0]:
                res["low_quality_notice"] = True
            logger.info(
                f"✅ [Instagram] Скачано через {strategy_name} ({height}p, {size / 1048576:.1f} МБ)"
            )
            return res

        if no_video:
            break
        if not size_error:
            break

    # Фото/карусель — только для обычных постов /p/, но не для /reel/.
    shortcode = extract_shortcode(clean_url)
    if shortcode:
        try:
            proxy_url = warp_proxy_module.warp.proxy_url if warp_proxy_module else None
            media_res = await _try_download_instagram_embed_media(
                shortcode,
                output_prefix,
                allow_photo=not is_reel,
                proxy_url=proxy_url,
                download_file_func=download_file_func,
                is_valid_mp4_func=is_valid_mp4_func,
                ffmpeg_path=ffmpeg_path
            )
            if media_res:
                return media_res
        except Exception as fb_err:
            logger.warning(f"Instagram media fallback failed: {fb_err}")
            last_error = last_error or fb_err

    if size_error and last_error is None:
        raise Exception("file_too_large: Instagram-видео больше 50 МБ даже в 480p")

    err_text = str(last_error or "").lower()
    if "login" in err_text or "rate-limit" in err_text or "rate limit" in err_text:
        raise Exception(f"instagram_login_required: {last_error}")
    if "no video in this post" in err_text:
        raise Exception("is_photo_post")
    if "available to everyone" in err_text or "certain audiences" in err_text:
        raise Exception("instagram_age_restricted")
    raise Exception(f"instagram_video_unavailable: {last_error}")


async def _try_download_instagram_embed_media(
    shortcode: str,
    output_prefix: str,
    allow_photo: bool,
    proxy_url: Optional[str],
    download_file_func,
    is_valid_mp4_func,
    ffmpeg_path: str = None
) -> Dict[str, Any]:
    """Вспомогательный парсер embed для скачивания постов /p/"""
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    direct_video_urls = []
    img_urls = []
    title = ""
    author = ""

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(embed_url, headers=EMBED_HEADERS, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    import html as py_html

                    owner_match = re.search(r'"username":\s*"([^"]+)"', html_text)
                    if owner_match:
                        author = py_html.unescape(owner_match.group(1)).strip()

                    cap_match = re.search(r'class="Caption"[^>]*>.*?<div[^>]*>([^<]+)<', html_text, re.DOTALL)
                    if cap_match:
                        title = py_html.unescape(cap_match.group(1)).strip()

                    # Поиск кандидатов на видео
                    candidates = []
                    for pat in (r'"video_url"\s*:\s*"([^"]+)"', r'"playable_url"\s*:\s*"([^"]+)"'):
                        candidates.extend(re.findall(pat, html_text))
                    for arr in re.findall(r'"video_versions"\s*:\s*\[(.*?)\]', html_text, re.S):
                        candidates.extend(re.findall(r'"url"\s*:\s*"([^"]+)"', arr))

                    seen_v = set()
                    for u in candidates:
                        cleaned = py_html.unescape(u).replace("\\/", "/").replace("\\u0026", "&")
                        if cleaned.startswith("http") and cleaned not in seen_v:
                            seen_v.add(cleaned)
                            direct_video_urls.append(cleaned)

                    # Картинки
                    raw_imgs = re.findall(r'"display_url":\s*"([^"]+)"', html_text)
                    if not raw_imgs:
                        raw_imgs = re.findall(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html_text)
                    for u in raw_imgs:
                        cleaned = py_html.unescape(u).replace("\\/", "/").replace("\\u0026", "&")
                        base = cleaned.split("?")[0]
                        if base not in img_urls:
                            img_urls.append(cleaned)
        except Exception as e:
            logger.debug(f"Embed HTML fetch error: {e}")

    # Пробуем прямое видео из embed
    for video_url in direct_video_urls:
        dest_video = f"{output_prefix}.mp4"
        try:
            await download_file_func(video_url, dest_video, proxy=proxy_url)
            if is_valid_mp4_func(dest_video):
                return {
                    "file_path": dest_video,
                    "platform": "instagram",
                    "title": title or "Instagram Reels",
                    "author": author or "Instagram",
                    "filesize": os.path.getsize(dest_video)
                }
        except Exception:
            if os.path.exists(dest_video):
                try: os.remove(dest_video)
                except OSError: pass

    if not allow_photo:
        raise Exception("instagram_video_unavailable")

    # Для обычных фотопостов
    if not img_urls:
        img_urls = [f"https://www.instagram.com/p/{shortcode}/media/?size=l"]

    if len(img_urls) > 1:
        album_photos = []
        for idx, u in enumerate(img_urls[:10]):
            dest = f"{output_prefix}_{idx}.jpg"
            try:
                await download_file_func(u, dest, proxy=proxy_url)
                if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                    album_photos.append(dest)
            except Exception:
                pass
        if len(album_photos) > 1:
            return {
                "is_album": True,
                "photos": album_photos,
                "platform": "instagram",
                "title": title or "Instagram Альбом",
                "author": author or "Instagram"
            }

    # Одиночное фото
    single_dest = f"{output_prefix}.jpg"
    await download_file_func(img_urls[0], single_dest, proxy=proxy_url)
    return {
        "file_path": single_dest,
        "is_photo": True,
        "title": title or "Instagram Фото",
        "author": author or "Instagram",
        "platform": "instagram",
        "filesize": os.path.getsize(single_dest) if os.path.exists(single_dest) else 0
    }
