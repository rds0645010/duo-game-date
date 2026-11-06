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


def _extract_instagram_metadata_sync(url: str, proxy_url: str = None, cookie_file: str = None) -> Optional[Dict[str, Any]]:
    """
    Извлекает полные метаданные публикации Instagram (видео, фото, карусели) через yt-dlp без скачивания.
    Временно отключает raise_no_formats, чтобы yt-dlp возвращал данные даже для публикаций только с фотографиями.
    """
    import yt_dlp
    from yt_dlp.extractor.instagram import InstagramIE

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'socket_timeout': 10,
        'http_headers': {
            'User-Agent': EMBED_HEADERS["User-Agent"],
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }
    if proxy_url:
        ydl_opts['proxy'] = proxy_url
    if cookie_file and os.path.exists(cookie_file):
        ydl_opts['cookiefile'] = cookie_file

    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        ydl_opts['impersonate'] = ImpersonateTarget('chrome')
    except Exception:
        pass

    captured_data = {}
    orig_extract = InstagramIE._real_extract
    def _patched_extract(self, u):
        orig_raise = self.raise_no_formats
        self.raise_no_formats = lambda *a, **kw: None
        try:
            res = orig_extract(self, u)
            captured_data["result"] = res
            return res
        finally:
            self.raise_no_formats = orig_raise

    InstagramIE._real_extract = _patched_extract
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                ydl.extract_info(url, download=False)
            except Exception:
                pass
        return captured_data.get("result")
    except Exception as e:
        logger.debug(f"Instagram yt-dlp metadata error: {e}")
        return captured_data.get("result")
    finally:
        InstagramIE._real_extract = orig_extract


async def _download_from_instagram_meta(
    info: Dict[str, Any],
    output_prefix: str,
    clean_url: str,
    proxy_url: Optional[str],
    download_file_func,
    is_valid_mp4_func = None
) -> Optional[Dict[str, Any]]:
    """
    Скачивает слайды карусели, фото или видео на основе структуры метаданных yt-dlp.
    Поддерживает:
    - Фото-карусели (альбомы) до 30 слайдов с параллельной загрузкой;
    - Видео-слайды и смешанные карусели (фото + видео);
    - Одиночные фотографии в максимальном разрешении;
    - Одиночные видео.
    """
    if not info:
        return None

    title = info.get("description") or info.get("title") or "Instagram Публикация"
    author = info.get("uploader") or info.get("channel") or "Instagram"
    likes = info.get("like_count") or 0
    comments = info.get("comment_count") or 0

    # 1. Сценарий: Карусель (альбом из нескольких слайдов)
    raw_entries = info.get("entries")
    if raw_entries:
        if not isinstance(raw_entries, list):
            try:
                raw_entries = list(raw_entries)
            except Exception:
                raw_entries = []
    if raw_entries and isinstance(raw_entries, list) and len(raw_entries) > 1:
        targets = raw_entries[:30]
        album_files = [None] * len(targets)

        async def _dl_slide(idx: int, item: Dict[str, Any]):
            if not item:
                return
            # Если в слайде есть видео
            v_formats = [f for f in item.get("formats", []) if f.get("url") and f.get("vcodec") != "none"]
            if v_formats:
                best_v = max(v_formats, key=lambda f: f.get("height") or 0)
                dest = f"{output_prefix}_{idx}.mp4"
                try:
                    await download_file_func(best_v["url"], dest, proxy=proxy_url)
                    if os.path.exists(dest) and (not is_valid_mp4_func or is_valid_mp4_func(dest)):
                        album_files[idx] = dest
                        return
                except Exception as dl_err:
                    logger.debug(f"Instagram carousel slide {idx} video download failed: {dl_err}")

            # Иначе берем лучшее фото (из thumbnails, url или display_url)
            thumbs = item.get("thumbnails", [])
            photo_url = None
            if thumbs:
                best_t = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
                photo_url = best_t.get("url")
            if not photo_url:
                photo_url = item.get("url") or item.get("display_url")
            if photo_url:
                dest = f"{output_prefix}_{idx}.jpg"
                try:
                    await download_file_func(photo_url, dest, proxy=proxy_url)
                    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                        album_files[idx] = dest
                        return
                except Exception as dl_err:
                    logger.debug(f"Instagram carousel slide {idx} photo download failed: {dl_err}")

        await asyncio.gather(*[_dl_slide(i, item) for i, item in enumerate(targets)])
        valid_files = [f for f in album_files if f]

        if len(valid_files) > 1:
            p_urls = []
            for t in targets:
                if isinstance(t, dict):
                    u = t.get("url") or t.get("display_url")
                    if not u and t.get("thumbnails"):
                        u = t["thumbnails"][-1].get("url")
                    if u:
                        p_urls.append(u)
            return {
                "is_album": True,
                "photos": valid_files,
                "photo_urls": p_urls,
                "file_path": valid_files[0],
                "platform": "instagram",
                "title": title,
                "author": author,
                "digg_count": likes,
                "comment_count": comments,
                "share_count": 0,
                "original_url": clean_url
            }
        elif len(valid_files) == 1:
            single = valid_files[0]
            is_vid = single.lower().endswith(".mp4")
            return {
                "file_path": single,
                "is_photo": not is_vid,
                "platform": "instagram",
                "title": title,
                "author": author,
                "filesize": os.path.getsize(single),
                "digg_count": likes,
                "comment_count": comments,
                "share_count": 0,
                "original_url": clean_url
            }

    # 2. Сценарий: Одиночное фото
    thumbs = info.get("thumbnails", [])
    if thumbs and not info.get("formats"):
        best_t = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        dest = f"{output_prefix}.jpg"
        try:
            await download_file_func(best_t["url"], dest, proxy=proxy_url)
            if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                return {
                    "file_path": dest,
                    "is_photo": True,
                    "title": title,
                    "author": author,
                    "platform": "instagram",
                    "filesize": os.path.getsize(dest),
                    "digg_count": likes,
                    "comment_count": comments,
                    "share_count": 0,
                    "original_url": clean_url
                }
        except Exception as dl_err:
            logger.debug(f"Instagram single photo download failed: {dl_err}")

    # 3. Сценарий: Одиночное видео
    v_formats = [f for f in info.get("formats", []) if f.get("url") and f.get("vcodec") != "none"]
    if v_formats:
        best_v = max(v_formats, key=lambda f: f.get("height") or 0)
        dest = f"{output_prefix}.mp4"
        try:
            await download_file_func(best_v["url"], dest, proxy=proxy_url)
            if os.path.exists(dest) and (not is_valid_mp4_func or is_valid_mp4_func(dest)):
                return {
                    "file_path": dest,
                    "platform": "instagram",
                    "title": title,
                    "author": author,
                    "filesize": os.path.getsize(dest),
                    "digg_count": likes,
                    "comment_count": comments,
                    "share_count": 0,
                    "original_url": clean_url
                }
        except Exception as dl_err:
            logger.debug(f"Instagram video stream download failed: {dl_err}")

    return None


async def download_instagram(
    clean_url: str,
    output_prefix: Optional[str] = None,
    ytdlp_sync_func=None,
    warp_proxy_module=None,
    download_file_func=None,
    is_valid_mp4_func=None,
    ffmpeg_path: str = None,
    progress_callback = None
) -> Dict[str, Any]:
    """
    Полностью независимый конвейер загрузки Instagram:
    - Извлечение метаданных yt-dlp с curl_cffi-имперсонацией
    - Локальный WARP прокси для анонимного запроса (logged-out Polaris)
    - Поддержка каруселей до 30 слайдов, фото и видео
    - Embed-fallback с поддержкой каруселей и фото для обычных постов
    """
    if not output_prefix:
        import tempfile, uuid
        output_prefix = os.path.join(tempfile.gettempdir(), f"ig_{uuid.uuid4().hex[:8]}")
    is_reel = is_reel_url(clean_url)
    loop = asyncio.get_running_loop()

    cookie_file = None
    try:
        import downloader
        cookie_file = downloader.setup_cookies_file("instagram")
    except Exception:
        pass

    strategies = []
    if warp_proxy_module and warp_proxy_module.warp.enabled and warp_proxy_module.warp.supported:
        try:
            if await warp_proxy_module.warp.ensure_ready() and warp_proxy_module.warp.proxy_url:
                strategies.append(("warp", warp_proxy_module.warp.proxy_url))
        except Exception as e:
            logger.warning(f"Instagram WARP init error: {e}")
    strategies.append(("direct", ""))

    last_error = None
    no_video = False

    # 1. Для обычных постов /p/ (не Reels) пробуем сначала быстро извлечь метаданные и фото/карусель
    if not is_reel:
        for strategy_name, strategy_proxy in strategies:
            try:
                info = await asyncio.to_thread(_extract_instagram_metadata_sync, clean_url, strategy_proxy, cookie_file)
                if info:
                    media_res = await _download_from_instagram_meta(
                        info, output_prefix, clean_url, strategy_proxy, download_file_func, is_valid_mp4_func
                    )
                    if media_res:
                        logger.info(f"✅ [Instagram] Фото/карусель получена через {strategy_name}")
                        return media_res
            except Exception as e:
                logger.debug(f"[Instagram] Metadata {strategy_name} error: {e}")

    # 2. Выполняем попытку yt-dlp для видео
    for strategy_name, strategy_proxy in strategies:
        try:
            socket_timeout = 8 if strategy_name == "direct" else 10
            res = await asyncio.to_thread(
                ytdlp_sync_func,
                clean_url,
                output_prefix,
                1280,
                platform="instagram",
                use_cookies=True,
                proxy_url=strategy_proxy,
                socket_timeout=socket_timeout,
                progress_callback=progress_callback,
                event_loop=loop,
            )
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            logger.warning(f"[Instagram] {strategy_name} download failed: {e}")
            if any(k in err_str for k in [
                "no video in this post",
                "there is no video",
                "requested format is not available",
                "no video formats found",
                "no formats found",
                "no video could be found",
                "there are no video formats",
                "is not a video",
                "no video stream"
            ]):
                no_video = True
                break
            if any(k in err_str for k in [
                "empty media response",
                "checkpoint_required",
                "restricted video",
                "not granting access"
            ]):
                break
            continue

        if not res or not res.get("file_path"):
            last_error = Exception("empty download result")
            continue

        size = res.get("filesize") or 0
        if size > 50 * 1024 * 1024:
            logger.warning(f"[Instagram] {strategy_name}: {size / 1048576:.1f} МБ > 50 МБ, пробуем 480p")
            try:
                if os.path.exists(res["file_path"]):
                    os.remove(res["file_path"])
                res = await asyncio.to_thread(
                    ytdlp_sync_func,
                    clean_url,
                    output_prefix,
                    480,
                    platform="instagram",
                    use_cookies=True,
                    proxy_url=strategy_proxy,
                    socket_timeout=socket_timeout,
                    progress_callback=progress_callback,
                    event_loop=loop,
                )
                if res and res.get("file_path") and (res.get("filesize") or 0) <= 50 * 1024 * 1024:
                    res["platform"] = "instagram"
                    res["low_quality_notice"] = True
                    return res
            except Exception as e_low:
                logger.warning(f"[Instagram] 480p retry failed: {e_low}")
            continue

        res["platform"] = "instagram"
        logger.info(
            f"✅ [Instagram] Скачано через {strategy_name} ({size / 1048576:.1f} МБ)"
        )
        return res

    # 3. Если видео не найдено — это фото или карусель! Пробуем метаданные еще раз со всеми стратегиями
    if no_video or not is_reel:
        for strategy_name, strategy_proxy in strategies:
            try:
                info = await asyncio.to_thread(_extract_instagram_metadata_sync, clean_url, strategy_proxy, cookie_file)
                if info:
                    media_res = await _download_from_instagram_meta(
                        info, output_prefix, clean_url, strategy_proxy, download_file_func, is_valid_mp4_func
                    )
                    if media_res:
                        logger.info(f"✅ [Instagram] Фото/карусель получена через {strategy_name} (фоллбэк)")
                        return media_res
            except Exception as e:
                logger.debug(f"[Instagram] Metadata fallback {strategy_name} error: {e}")

    # 4. Резервный Embed-фоллбэк для постов /p/
    shortcode = extract_shortcode(clean_url)
    if shortcode:
        try:
            proxy_url = warp_proxy_module.warp.proxy_url if warp_proxy_module else None
            media_res = await _try_download_instagram_embed_media(
                shortcode,
                output_prefix,
                allow_photo=True,
                proxy_url=proxy_url,
                download_file_func=download_file_func,
                is_valid_mp4_func=is_valid_mp4_func,
                ffmpeg_path=ffmpeg_path
            )
            if media_res:
                return media_res
        except Exception as fb_err:
            logger.warning(f"Instagram embed fallback failed: {fb_err}")
            last_error = last_error or fb_err

    err_text = str(last_error or "").lower()
    is_login_wall = any(k in err_text for k in [
        "empty media response", "login", "logged-in", "rate-limit",
        "rate limit", "media cannot be found", "not granting access",
        "checkpoint_required", "restricted video"
    ])
    if is_login_wall:
        raise Exception(f"instagram_login_required: {last_error}")
    if any(k in err_text for k in [
        "available to everyone", "certain audiences", "age-restricted",
        "confirm your age"
    ]):
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
    """Вспомогательный парсер embed для скачивания постов /p/ и Reels"""
    embed_urls = [
        f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
        f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
    ]
    direct_video_urls = []
    img_urls = []
    title = ""
    author = ""

    async with aiohttp.ClientSession() as session:
        for embed_url in embed_urls:
            # Сначала с прокси (если есть), затем напрямую при неудаче
            proxies_to_try = [proxy_url] if proxy_url else [None]
            if proxy_url:
                proxies_to_try.append(None)

            for cur_proxy in proxies_to_try:
                try:
                    async with session.get(
                        embed_url,
                        headers=EMBED_HEADERS,
                        proxy=cur_proxy,
                        timeout=aiohttp.ClientTimeout(total=4)
                    ) as resp:
                        if resp.status == 200:
                            html_text = await resp.text()
                            import html as py_html

                            owner_match = re.search(r'"username":\s*"([^"]+)"', html_text)
                            if owner_match:
                                author = py_html.unescape(owner_match.group(1)).strip()

                            cap_match = re.search(r'class="Caption"[^>]*>.*?<div[^>]*>([^<]+)<', html_text, re.DOTALL)
                            if cap_match:
                                title = py_html.unescape(cap_match.group(1)).strip()

                            candidates = []
                            for pat in (
                                r'"video_url"\s*:\s*"([^"]+)"',
                                r'"playable_url"\s*:\s*"([^"]+)"',
                                r'video_url&quot;:&quot;([^&]+)&quot;',
                                r'"playback_url"\s*:\s*"([^"]+)"',
                                r'src="(https://[^"]+?\.mp4[^"]*)"',
                            ):
                                candidates.extend(re.findall(pat, html_text))
                            for arr in re.findall(r'"video_versions"\s*:\s*\[(.*?)\]', html_text, re.S):
                                candidates.extend(re.findall(r'"url"\s*:\s*"([^"]+)"', arr))

                            seen_v = set()
                            for u in candidates:
                                cleaned = py_html.unescape(u).replace("\\/", "/").replace("\\u0026", "&")
                                if cleaned.startswith("http") and cleaned not in seen_v:
                                    seen_v.add(cleaned)
                                    direct_video_urls.append(cleaned)

                            raw_imgs = []
                            for m in re.finditer(r'"display_url"\s*:\s*"([^"]+)"', html_text):
                                raw_imgs.append(m.group(1))
                            for m in re.finditer(r'<img[^>]+class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html_text):
                                raw_imgs.append(m.group(1))
                            for m in re.finditer(r'"(https://[^"]+?cdninstagram\.com/[^"]+?\.jpg[^"]*)"', html_text):
                                raw_imgs.append(m.group(1))
                            for m in re.finditer(r'"(https://[^"]+?fbcdn\.net/[^"]+?\.jpg[^"]*)"', html_text):
                                raw_imgs.append(m.group(1))

                            for u in raw_imgs:
                                cleaned = py_html.unescape(u).replace("\\/", "/").replace("\\u0026", "&")
                                base = cleaned.split("?")[0]
                                if base not in [x.split("?")[0] for x in img_urls]:
                                    img_urls.append(cleaned)

                            if direct_video_urls:
                                break
                except Exception as e:
                    logger.debug(f"Embed HTML fetch error for {embed_url} (proxy={cur_proxy}): {e}")
            if direct_video_urls:
                break

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
                    "filesize": os.path.getsize(dest_video),
                    "digg_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                    "original_url": f"https://www.instagram.com/p/{shortcode}/"
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
        targets = img_urls[:30]
        album_photos = [None] * len(targets)

        async def _dl_embed_slide(idx: int, u: str):
            dest = f"{output_prefix}_{idx}.jpg"
            try:
                await download_file_func(u, dest, proxy=proxy_url)
                if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                    album_photos[idx] = dest
            except Exception:
                pass

        await asyncio.gather(*[_dl_embed_slide(i, u) for i, u in enumerate(targets)])
        valid_photos = [p for p in album_photos if p]

        if len(valid_photos) > 1:
            return {
                "is_album": True,
                "photos": valid_photos,
                "photo_urls": targets,
                "file_path": valid_photos[0],
                "platform": "instagram",
                "title": title or "Instagram Альбом",
                "author": author or "Instagram",
                "digg_count": 0,
                "comment_count": 0,
                "share_count": 0,
                "original_url": f"https://www.instagram.com/p/{shortcode}/"
            }
        elif len(valid_photos) == 1:
            single_dest = valid_photos[0]
            return {
                "file_path": single_dest,
                "is_photo": True,
                "title": title or "Instagram Фото",
                "author": author or "Instagram",
                "platform": "instagram",
                "filesize": os.path.getsize(single_dest),
                "digg_count": 0,
                "comment_count": 0,
                "share_count": 0,
                "original_url": f"https://www.instagram.com/p/{shortcode}/"
            }

    # Одиночное фото
    single_dest = f"{output_prefix}.jpg"
    try:
        await download_file_func(img_urls[0], single_dest, proxy=proxy_url)
        if os.path.exists(single_dest) and os.path.getsize(single_dest) > 1000:
            return {
                "file_path": single_dest,
                "is_photo": True,
                "title": title or "Instagram Фото",
                "author": author or "Instagram",
                "platform": "instagram",
                "filesize": os.path.getsize(single_dest),
                "digg_count": 0,
                "comment_count": 0,
                "share_count": 0,
                "original_url": f"https://www.instagram.com/p/{shortcode}/"
            }
    except Exception as e:
        logger.debug(f"Single photo embed download failed: {e}")

    raise Exception("is_photo_post")
