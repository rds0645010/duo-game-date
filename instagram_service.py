import os
import re
import asyncio
import logging
import aiohttp
from typing import Optional, Dict, Any, List

import http_pool

logger = logging.getLogger("instagram_service")

# Regex для Instagram (Reels, Posts, Stories/Share, TV, профильные /username/reel/...)
INSTAGRAM_REGEX = re.compile(
    r'https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/(?:(?:[A-Za-z0-9._]+/)?(?:reel|reels|p|tv)|share(?:/(?:reel|p))?)/[^\s]+'
)

INSTAGRAM_HEIGHTS = [1280, 720, 480]

EMBED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
}


def patch_instagram_music_extraction():
    """
    Патчит yt-dlp InstagramIE, чтобы извлекать информацию о музыкальном треке и авторе (clips_metadata, music_metadata),
    которую yt-dlp по умолчанию не извлекает для Reels.
    """
    try:
        from yt_dlp.extractor.instagram import InstagramIE
        if getattr(InstagramIE, "_music_patched", False):
            return
        orig_extract_product = InstagramIE._extract_product

        def _custom_extract_product(self, product_info, *args, **kwargs):
            res = orig_extract_product(self, product_info, *args, **kwargs)
            try:
                target_dict = res if isinstance(res, dict) else {}
                p_info = product_info
                if isinstance(p_info, list) and p_info:
                    p_info = p_info[0]
                if isinstance(p_info, dict) and not target_dict.get("track"):
                    t = None
                    a = None
                    clips = p_info.get("clips_metadata") or {}
                    audio_model = clips.get("audio_type_model") or clips.get("music_info") or {}
                    m_info = audio_model.get("music_info") or audio_model.get("music_asset_info") or clips.get("music_info") or {}
                    asset = m_info.get("music_asset_info") or m_info
                    t = asset.get("title") or asset.get("song_name")
                    a = asset.get("display_artist") or asset.get("artist_name")
                    if not t:
                        orig = audio_model.get("original_sound_info") or clips.get("original_sound_info") or {}
                        t = orig.get("original_audio_title")
                        a = orig.get("ig_artist", {}).get("username") or orig.get("ig_artist", {}).get("full_name")
                    if not t:
                        music_meta = p_info.get("music_metadata") or {}
                        m_meta_info = music_meta.get("music_info") or {}
                        asset2 = m_meta_info.get("music_asset_info") or m_meta_info
                        t = asset2.get("title") or asset2.get("song_name")
                        a = asset2.get("display_artist") or asset2.get("artist_name")
                    if not t:
                        c_attr = p_info.get("clips_music_attribution_info") or {}
                        t = c_attr.get("song_name")
                        a = c_attr.get("artist_name")
                    if not t:
                        edges = (p_info.get("edge_media_to_music_attribution") or {}).get("edges") or []
                        if edges and isinstance(edges, list) and isinstance(edges[0], dict):
                            node = edges[0].get("node") or {}
                            t = node.get("song_name")
                            a = node.get("artist_name")
                    if t:
                        target_dict["track"] = str(t).strip()
                        if a:
                            target_dict["artist"] = str(a).strip()
            except Exception:
                pass
            return res

        InstagramIE._extract_product = _custom_extract_product
        InstagramIE._music_patched = True
    except Exception as e:
        logger.debug(f"Failed to patch InstagramIE music extraction: {e}")


patch_instagram_music_extraction()


def is_reel_url(url: str) -> bool:
    return bool(re.search(r'(?:instagram\.com|instagr\.am)/(?:[A-Za-z0-9._]+/)?(?:reel|reels|tv|share/reel)/', url or "", re.IGNORECASE))


def extract_shortcode(url: str) -> Optional[str]:
    m = re.search(r'(?:instagram\.com|instagr\.am)/(?:[A-Za-z0-9._]+/)?(?:reel|p|reels|tv)/([A-Za-z0-9_-]+)', url or "")
    return m.group(1) if m else None


async def _resolve_instagram_share_url(url: str, proxy: Optional[str] = None) -> str:
    """Разворачивает /share/... ссылки Instagram через редирект до канонического /reel/ или /p/ URL."""
    if "/share/" not in (url or "").lower():
        return url
    attempts = [None]
    if proxy and proxy not in attempts:
        attempts.append(proxy)
    try:
        session = await http_pool.get_http_session()
        for p in attempts:
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                    headers=EMBED_HEADERS,
                    proxy=p,
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    target = str(resp.url)
                    if extract_shortcode(target):
                        clean = target.split("?")[0]
                        logger.info(f"🔗 [Instagram] Развернута share-ссылка: {url} -> {clean}")
                        return clean
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Instagram share resolve error: {e}")
    return url


def _clean_ig_url(raw: str) -> str:
    import html as py_html
    cleaned = py_html.unescape(str(raw or ""))
    cleaned = (
        cleaned.replace("\\\\/", "/")
        .replace("\\/", "/")
        .replace("\\\\u0026", "&")
        .replace("\\u0026", "&")
        .replace("\\u0025", "%")
        .replace("\\u003d", "=")
        .replace("\\u003f", "?")
        .rstrip("\\")
        .strip()
    )
    return cleaned


def _extract_embed_video_urls(html_text: str) -> List[str]:
    """Извлекает прямые MP4/видео URL из HTML embed-страницы (включая экранированный JSON)."""
    if not html_text:
        return []
    unescaped = html_text.replace('\\\\"', '"').replace('\\"', '"')
    candidates = []
    for text_var in (html_text, unescaped):
        for pat in (
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"playable_url"\s*:\s*"([^"]+)"',
            r'"playback_url"\s*:\s*"([^"]+)"',
            r'video_url&quot;:&quot;([^&]+)&quot;',
            r'<(?:video|source)[^>]+src="(https://[^"]+?\.mp4[^"]*)"',
        ):
            candidates.extend(re.findall(pat, text_var))
        for arr in re.findall(r'"video_versions"\s*:\s*\[(.*?)\]', text_var, re.S):
            candidates.extend(re.findall(r'"url"\s*:\s*"([^"]+)"', arr))

    result = []
    seen = set()
    for u in candidates:
        cleaned = _clean_ig_url(u)
        if not cleaned.startswith(("http://", "https://")):
            continue
        base_lower = cleaned.split("?")[0].lower()
        if base_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _extract_embed_image_urls(html_text: str) -> List[str]:
    """Извлекает только контентные изображения публикации, отсекая аватарки и статику."""
    if not html_text:
        return []
    unescaped = html_text.replace('\\\\"', '"').replace('\\"', '"')
    raw_imgs = []
    for text_var in (html_text, unescaped):
        raw_imgs.extend(re.findall(r'"display_url"\s*:\s*"([^"]+)"', text_var))
        raw_imgs.extend(re.findall(r'<img[^>]+class="[^"]*EmbeddedMediaImage[^"]*"[^>]+src="([^"]+)"', text_var))
        raw_imgs.extend(re.findall(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', text_var))

    result = []
    seen_bases = set()
    for u in raw_imgs:
        cleaned = _clean_ig_url(u)
        if not cleaned.startswith(("http://", "https://")):
            continue
        low = cleaned.lower()
        if any(bad in low for bad in ("t51.2885-19", "s150x150", "s320x320", "profile_pic", "static.cdninstagram.com", "/rsrc.php/", "instagram.com/static/")):
            continue
        base = cleaned.split("?")[0]
        if base not in seen_bases:
            seen_bases.add(base)
            result.append(cleaned)
    return result


def _embed_html_looks_like_video(html_text: str) -> bool:
    if not html_text:
        return False
    unescaped = html_text.replace('\\\\"', '"').replace('\\"', '"')
    if '"GraphSidecar"' in unescaped:
        return False
    return any(
        marker in unescaped
        for marker in (
            '"is_video":true',
            '"__typename":"GraphVideo"',
            '"product_type":"clips"',
            '"media_type":2',
            "EmbeddedMediaVideo",
            "<video ",
        )
    )


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
        session = await http_pool.get_http_session()
        if True:
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

                    # Музыка
                    m_track = None
                    m_artist = None
                    music_match = re.search(r'"music_asset_info":\s*\{[^}]*"title":\s*"([^"]+)"[^}]*"display_artist":\s*"([^"]+)"', text)
                    if music_match:
                        m_track = py_html.unescape(music_match.group(1)).strip()
                        m_artist = py_html.unescape(music_match.group(2)).strip()
                    else:
                        orig_sound_match = re.search(r'"original_sound_info":\s*\{[^}]*"original_audio_title":\s*"([^"]+)"', text)
                        if orig_sound_match:
                            m_track = py_html.unescape(orig_sound_match.group(1)).strip()

                    m_inf = {"title": m_track, "author": m_artist or ""} if m_track else None

                    # Прямое видео
                    video_matches = _extract_embed_video_urls(text)
                    if video_matches:
                        v_url = video_matches[0]
                        disp_matches = _extract_embed_image_urls(text)
                        thumb = disp_matches[0] if disp_matches else f"https://www.instagram.com/p/{shortcode}/media/?size=l"
                        return {
                            "success": True,
                            "platform": "instagram",
                            "is_video": True,
                            "video_url": v_url,
                            "title": title or "Instagram Reels",
                            "author": author or "Instagram",
                            "thumbnail": thumb,
                            "music": m_track,
                            "music_info": m_inf
                        }

                    if _embed_html_looks_like_video(text):
                        return None

                    # Прямое фото (только для обычных фотопостов, не для Reels)
                    photo_matches = _extract_embed_image_urls(text)

                    if photo_matches and not is_reel:
                        p_url = photo_matches[0]
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
    is_valid_mp4_func = None,
    progress_callback = None,
    allow_photo: bool = True,
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

    info_is_video = bool(
        info.get("duration")
        or info.get("video_url")
        or info.get("is_video")
        or info.get("media_type") == 2
        or str(info.get("product_type") or "").lower() in ("clips", "igtv", "feed_video")
        or "video" in str(info.get("__typename") or "").lower()
    )

    title = info.get("description") or info.get("title") or "Instagram Публикация"
    author = info.get("uploader") or info.get("channel") or "Instagram"
    likes = info.get("like_count") or 0
    comments = info.get("comment_count") or 0
    track_name = info.get("track") or info.get("song")
    artist_name = info.get("artist")
    music_info = {"title": track_name, "author": artist_name or ""} if track_name else None

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
        total_slides = len(targets)
        downloaded_slides = 0
        slide_lock = asyncio.Lock()

        def _expected_slide_path(idx: int, item: Dict[str, Any]) -> str:
            v_formats = [f for f in (item or {}).get("formats", []) if f.get("url") and f.get("vcodec") != "none"]
            ext = "mp4" if v_formats else "jpg"
            return f"{output_prefix}_{idx}.{ext}"

        expected_paths = [_expected_slide_path(i, it) for i, it in enumerate(targets)]
        p_urls = []
        for t in targets:
            u = None
            if isinstance(t, dict):
                u = t.get("url") or t.get("display_url")
                if not u and t.get("thumbnails"):
                    u = t["thumbnails"][-1].get("url")
            p_urls.append(u or "")

        async def _dl_slide(idx: int, item: Dict[str, Any], notify_progress: bool = False):
            nonlocal downloaded_slides
            if not item:
                return
            try:
                # Если в слайде есть видео (предпочитаем muxed со звуком)
                v_formats = [f for f in item.get("formats", []) if f.get("url") and f.get("vcodec") != "none"]
                if v_formats:
                    muxed_v = [f for f in v_formats if f.get("acodec") not in (None, "none")]
                    best_v = max(muxed_v or v_formats, key=lambda f: f.get("height") or 0)
                    dest = f"{output_prefix}_{idx}.mp4"
                    tmp_dest = f"{dest}.part"
                    try:
                        await download_file_func(best_v["url"], tmp_dest, proxy=proxy_url)
                        if os.path.exists(tmp_dest) and (not is_valid_mp4_func or is_valid_mp4_func(tmp_dest)):
                            os.replace(tmp_dest, dest)
                            album_files[idx] = dest
                            return
                    except Exception as dl_err:
                        logger.debug(f"Instagram carousel slide {idx} video download failed: {dl_err}")
                    finally:
                        if os.path.exists(tmp_dest):
                            try:
                                os.remove(tmp_dest)
                            except OSError:
                                pass

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
                    tmp_dest = f"{dest}.part"
                    try:
                        await download_file_func(photo_url, tmp_dest, proxy=proxy_url)
                        if os.path.exists(tmp_dest) and os.path.getsize(tmp_dest) > 1000:
                            os.replace(tmp_dest, dest)
                            album_files[idx] = dest
                            return
                    except Exception as dl_err:
                        logger.debug(f"Instagram carousel slide {idx} photo download failed: {dl_err}")
                    finally:
                        if os.path.exists(tmp_dest):
                            try:
                                os.remove(tmp_dest)
                            except OSError:
                                pass
            finally:
                async with slide_lock:
                    downloaded_slides += 1
                    if notify_progress and progress_callback:
                        try:
                            cb = progress_callback(downloaded_slides, total_slides, is_slide=True)
                            if asyncio.iscoroutine(cb):
                                await cb
                        except Exception:
                            pass

        # Fast-Track: качаем сначала 1-й слайд и отдаём пользователю, остальные — в фоне
        await _dl_slide(0, targets[0], notify_progress=True)
        if album_files[0]:
            expected_paths[0] = album_files[0]
            async def _bg_dl_remaining():
                sem = asyncio.Semaphore(4)
                async def _bounded(i: int, it: Dict[str, Any]):
                    async with sem:
                        await _dl_slide(i, it, notify_progress=False)
                await asyncio.gather(*[_bounded(i, targets[i]) for i in range(1, len(targets))], return_exceptions=True)

            bg_task = asyncio.create_task(_bg_dl_remaining())
            return {
                "is_album": True,
                "photos": expected_paths,
                "photo_urls": [u for u in p_urls if u],
                "file_path": album_files[0],
                "fast_track": True,
                "_bg_task": bg_task,
                "platform": "instagram",
                "title": title,
                "author": author,
                "digg_count": likes,
                "comment_count": comments,
                "share_count": 0,
                "music": track_name,
                "music_info": music_info,
                "original_url": clean_url
            }

        # Резерв, если 0-й слайд не скачался: догружаем остальные синхронно
        await asyncio.gather(*[_dl_slide(i, targets[i]) for i in range(1, len(targets))])
        valid_files = [f for f in album_files if f]

        if len(valid_files) > 1:
            return {
                "is_album": True,
                "photos": valid_files,
                "photo_urls": [u for u in p_urls if u],
                "file_path": valid_files[0],
                "platform": "instagram",
                "title": title,
                "author": author,
                "digg_count": likes,
                "comment_count": comments,
                "share_count": 0,
                "music": track_name,
                "music_info": music_info,
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
                "music": track_name,
                "music_info": music_info,
                "original_url": clean_url
            }

    # 2. Сценарий: Одиночное фото (строго запрещён для видео и при allow_photo=False)
    thumbs = info.get("thumbnails", [])
    if allow_photo and not info_is_video and thumbs and not info.get("formats"):
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
                    "music": track_name,
                    "music_info": music_info,
                    "original_url": clean_url
                }
        except Exception as dl_err:
            logger.debug(f"Instagram single photo download failed: {dl_err}")

    # 3. Сценарий: Одиночное видео (только muxed со звуком; DASH без звука отдаём yt-dlp+ffmpeg)
    v_formats = [f for f in info.get("formats", []) if f.get("url") and f.get("vcodec") != "none"]
    if v_formats:
        muxed_v = [f for f in v_formats if f.get("acodec") not in (None, "none")]
        if not muxed_v:
            return None
        best_v = max(muxed_v, key=lambda f: f.get("height") or 0)
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
                    "music": track_name,
                    "music_info": music_info,
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
    loop = asyncio.get_running_loop()

    # Instagram идёт напрямую; WARP используем только как запасной вариант, если он уже запущен (не ждём его старта)
    warp_proxy_url = warp_proxy_module.warp.proxy_url if (warp_proxy_module and hasattr(warp_proxy_module, "warp")) else None

    # 0. Разворачиваем /share/... ссылки до канонического /reel/ или /p/
    clean_url = await _resolve_instagram_share_url(clean_url, proxy=warp_proxy_url)
    is_reel = is_reel_url(clean_url)

    cookie_file = None
    try:
        import downloader
        cookie_file = downloader.setup_cookies_file("instagram")
    except Exception:
        pass

    strategies = [("direct", "")]
    if warp_proxy_url:
        strategies.append(("warp", warp_proxy_url))

    last_error = None
    no_video = False

    # 1. Для обычных постов /p/ (не Reels) пробуем сначала анонимно извлечь метаданные и фото/карусель (не тратим куки)
    if not is_reel:
        for strategy_name, strategy_proxy in strategies:
            try:
                info = await asyncio.to_thread(_extract_instagram_metadata_sync, clean_url, strategy_proxy, None)
                if info:
                    media_res = await _download_from_instagram_meta(
                        info,
                        output_prefix,
                        clean_url,
                        strategy_proxy,
                        download_file_func,
                        is_valid_mp4_func,
                        progress_callback=progress_callback,
                        allow_photo=True,
                    )
                    if media_res:
                        logger.info(f"✅ [Instagram] Фото/карусель получена через {strategy_name}")
                        return media_res
            except Exception as e:
                logger.debug(f"[Instagram] Metadata {strategy_name} error: {e}")

    async def _run_ytdlp_pass(use_cookies_flag: bool) -> Optional[Dict[str, Any]]:
        nonlocal last_error, no_video
        for strategy_name, strategy_proxy in strategies:
            try:
                socket_timeout = 8 if strategy_name == "direct" else 10
                res = await asyncio.to_thread(
                    ytdlp_sync_func,
                    clean_url,
                    output_prefix,
                    1280,
                    platform="instagram",
                    use_cookies=use_cookies_flag,
                    proxy_url=strategy_proxy,
                    socket_timeout=socket_timeout,
                    progress_callback=progress_callback,
                    event_loop=loop,
                )
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                logger.warning(f"[Instagram] {strategy_name} (cookies={use_cookies_flag}) download failed: {e}")
                if not is_reel and any(k in err_str for k in [
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
                if "checkpoint_required" in err_str:
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
                        use_cookies=use_cookies_flag,
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
                f"✅ [Instagram] Скачано через {strategy_name} (cookies={use_cookies_flag}, {size / 1048576:.1f} МБ)"
            )
            return res
        return None

    # 2. Сначала пробуем yt-dlp анонимно (без cookies), чтобы публичные ролики не нагружали сессию и не вызывали checkpoint
    yt_res = await _run_ytdlp_pass(False)
    if yt_res:
        return yt_res

    anon_error = last_error

    # 2b. Если анонимно не отдали (18+, login wall, закрытый пост) и есть cookies — пробуем с cookies
    if cookie_file and not no_video:
        logger.info("[Instagram] Анонимно не удалось, пробую yt-dlp с cookies (для 18+/закрытых постов)...")
        yt_cookie_res = await _run_ytdlp_pass(True)
        if yt_cookie_res:
            return yt_cookie_res
        if anon_error and last_error and "400" in str(last_error).lower() and "bad request" in str(last_error).lower():
            # Сохраняем исходную причину (например 18+ / login_required), если cookies протухли с HTTP 400
            last_error = anon_error

    # 3. Если это НЕ Reel (обычный пост /p/) — пробуем метаданные для фото/карусели
    if not is_reel:
        for strategy_name, strategy_proxy in strategies:
            try:
                info = await asyncio.to_thread(_extract_instagram_metadata_sync, clean_url, strategy_proxy, cookie_file)
                if info:
                    media_res = await _download_from_instagram_meta(
                        info,
                        output_prefix,
                        clean_url,
                        strategy_proxy,
                        download_file_func,
                        is_valid_mp4_func,
                        progress_callback=progress_callback,
                        allow_photo=True,
                    )
                    if media_res:
                        logger.info(f"✅ [Instagram] Фото/карусель получена через {strategy_name} (фоллбэк)")
                        return media_res
            except Exception as e:
                logger.debug(f"[Instagram] Metadata fallback {strategy_name} error: {e}")

    # 4. Резервный Embed-фоллбэк (для Reels фото строго запрещено!)
    shortcode = extract_shortcode(clean_url)
    if shortcode:
        try:
            media_res = await _try_download_instagram_embed_media(
                shortcode,
                output_prefix,
                allow_photo=not is_reel,
                proxy_url=warp_proxy_url,
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


def _fetch_embed_html_cffi(embed_url: str, proxy_url: Optional[str] = None) -> Optional[str]:
    """Запасной запрос embed-страницы через curl_cffi с TLS-отпечатком Chrome."""
    try:
        from curl_cffi import requests as cffi_requests
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        resp = cffi_requests.get(
            embed_url,
            headers=EMBED_HEADERS,
            impersonate="chrome",
            proxies=proxies,
            timeout=6,
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception as e:
        logger.debug(f"curl_cffi embed fetch error for {embed_url}: {e}")
    return None


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
    m_track = None
    m_artist = None
    detected_video_post = False

    session = await http_pool.get_http_session()
    if True:
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

                            # Музыка
                            m_track = None
                            m_artist = None
                            music_match = re.search(r'"music_asset_info":\s*\{[^}]*"title":\s*"([^"]+)"[^}]*"display_artist":\s*"([^"]+)"', html_text)
                            if music_match:
                                m_track = py_html.unescape(music_match.group(1)).strip()
                                m_artist = py_html.unescape(music_match.group(2)).strip()
                            else:
                                orig_sound_match = re.search(r'"original_sound_info":\s*\{[^}]*"original_audio_title":\s*"([^"]+)"', html_text)
                                if orig_sound_match:
                                    m_track = py_html.unescape(orig_sound_match.group(1)).strip()

                            if _embed_html_looks_like_video(html_text):
                                detected_video_post = True

                            for cleaned in _extract_embed_video_urls(html_text):
                                if cleaned not in direct_video_urls:
                                    direct_video_urls.append(cleaned)

                            for cleaned in _extract_embed_image_urls(html_text):
                                base = cleaned.split("?")[0]
                                if base not in [x.split("?")[0] for x in img_urls]:
                                    img_urls.append(cleaned)

                            if direct_video_urls:
                                break
                except Exception as e:
                    logger.debug(f"Embed HTML fetch error for {embed_url} (proxy={cur_proxy}): {e}")
            if not direct_video_urls:
                cffi_html = await asyncio.to_thread(_fetch_embed_html_cffi, embed_url, proxy_url)
                if cffi_html:
                    if _embed_html_looks_like_video(cffi_html):
                        detected_video_post = True
                    for cleaned in _extract_embed_video_urls(cffi_html):
                        if cleaned not in direct_video_urls:
                            direct_video_urls.append(cleaned)
                    for cleaned in _extract_embed_image_urls(cffi_html):
                        base = cleaned.split("?")[0]
                        if base not in [x.split("?")[0] for x in img_urls]:
                            img_urls.append(cleaned)
            if direct_video_urls:
                break

    # Пробуем прямое видео из embed (сначала через proxy, затем напрямую)
    video_proxies = [proxy_url, None] if proxy_url else [None]
    for video_url in direct_video_urls:
        dest_video = f"{output_prefix}.mp4"
        for cur_v_proxy in video_proxies:
            try:
                await download_file_func(video_url, dest_video, proxy=cur_v_proxy)
                if os.path.exists(dest_video) and (not is_valid_mp4_func or is_valid_mp4_func(dest_video)):
                    m_inf = {"title": m_track, "author": m_artist or ""} if m_track else None
                    return {
                        "file_path": dest_video,
                        "platform": "instagram",
                        "title": title or "Instagram Reels",
                        "author": author or "Instagram",
                        "filesize": os.path.getsize(dest_video),
                        "digg_count": 0,
                        "comment_count": 0,
                        "share_count": 0,
                        "music": m_track,
                        "music_info": m_inf,
                        "original_url": f"https://www.instagram.com/p/{shortcode}/"
                    }
            except Exception:
                pass
            if os.path.exists(dest_video):
                try:
                    os.remove(dest_video)
                except OSError:
                    pass

    if not allow_photo or detected_video_post:
        raise Exception("instagram_video_unavailable")

    # Для обычных фотопостов
    if not img_urls:
        img_urls = [f"https://www.instagram.com/p/{shortcode}/media/?size=l"]

    if len(img_urls) > 1:
        targets = img_urls[:30]
        expected_paths = [f"{output_prefix}_{idx}.jpg" for idx in range(len(targets))]
        album_photos = [None] * len(targets)

        async def _dl_embed_slide(idx: int, u: str):
            dest = expected_paths[idx]
            tmp_dest = f"{dest}.part"
            try:
                await download_file_func(u, tmp_dest, proxy=proxy_url)
                if os.path.exists(tmp_dest) and os.path.getsize(tmp_dest) > 1000:
                    os.replace(tmp_dest, dest)
                    album_photos[idx] = dest
            except Exception:
                pass
            finally:
                if os.path.exists(tmp_dest):
                    try:
                        os.remove(tmp_dest)
                    except OSError:
                        pass

        await _dl_embed_slide(0, targets[0])
        if album_photos[0]:
            async def _bg_dl_embed():
                sem = asyncio.Semaphore(4)
                async def _bounded(i: int, u: str):
                    async with sem:
                        await _dl_embed_slide(i, u)
                await asyncio.gather(*[_bounded(i, targets[i]) for i in range(1, len(targets))], return_exceptions=True)

            bg_task = asyncio.create_task(_bg_dl_embed())
            return {
                "is_album": True,
                "photos": expected_paths,
                "photo_urls": targets,
                "file_path": album_photos[0],
                "fast_track": True,
                "_bg_task": bg_task,
                "platform": "instagram",
                "title": title or "Instagram Альбом",
                "author": author or "Instagram",
                "digg_count": 0,
                "comment_count": 0,
                "share_count": 0,
                "original_url": f"https://www.instagram.com/p/{shortcode}/"
            }

        await asyncio.gather(*[_dl_embed_slide(i, targets[i]) for i in range(1, len(targets))])
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
