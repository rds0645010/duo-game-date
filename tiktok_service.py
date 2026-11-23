import os
import re
import uuid
import html as py_html
import base64
import tempfile
import asyncio
import logging
import aiohttp
from typing import Optional, Dict, Any, List

import http_pool

logger = logging.getLogger("tiktok_service")

# Regex для распознавания ссылок TikTok
TIKTOK_REGEX = re.compile(r'https?://(?:(?:www|m|lite|v[mt])\.)?tiktok\.com/[^\s]+')

# Зеркала и эндпоинты API TikWM
TIKWM_ENDPOINTS = [
    "https://tikwm.com/api/",
    "https://www.tikwm.com/api/",
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def is_valid_mp4_file(file_path: str, allow_audio: bool = False) -> bool:
    """
    Проверяет по бинарным сигнатурам (magic bytes), является ли файл видео MP4/WebM/MKV.
    """
    if not file_path or not os.path.exists(file_path):
        return False
    if os.path.getsize(file_path) < 16 * 1024:
        return False
    try:
        with open(file_path, "rb") as f:
            header = f.read(32)
        if b"ftyp" in header[:16] or header.startswith(b"\x1a\x45\xdf\xa3"):
            return True
        if allow_audio and (
            header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"OggS")
        ):
            return True
    except Exception:
        pass
    return False


async def _parse_ssstik(url: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Высоконадежный парсер TikTok через SSSTik (для видео, фото-слайдшоу, каруселей и музыки).
    Работает мгновенно (< 1 сек), поддерживает WARP-прокси и динамический s_tt токен.
    """
    clean_target = url.split("?")[0] if ("/photo/" in url or "/video/" in url) else url
    post_data = {"id": clean_target, "locale": "en", "tt": "0"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Hx-Request": "true",
        "Hx-Target": "target",
        "Hx-Current-Url": "https://ssstik.io/en",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://ssstik.io",
        "Referer": "https://ssstik.io/en"
    }
    try:
        session = await http_pool.get_http_session()
        if True:
            # Если прямой запрос не удался или нужен динамический токен s_tt
            resp_text = None
            try:
                async with session.post(
                    "https://ssstik.io/abc?url=dl",
                    data=post_data,
                    headers=headers,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=6.0)
                ) as resp:
                    if resp.status == 200:
                        resp_text = await resp.text()
            except Exception as pe:
                logger.debug(f"SSSTik post tt=0 failed: {pe}")

            # Если ничего не вернулось или ответ пустой — запрашиваем страницу для получения свежего токена s_tt
            if not resp_text or "tikcdn.io" not in resp_text:
                try:
                    async with session.get(
                        "https://ssstik.io/en",
                        headers={"User-Agent": headers["User-Agent"]},
                        proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=4.0)
                    ) as r_init:
                        if r_init.status == 200:
                            html_init = await r_init.text()
                            m_tt = re.search(r's_tt\s*=\s*[\'"]([^\'"]+)[\'"]', html_init)
                            if m_tt:
                                post_data["tt"] = m_tt.group(1)
                                async with session.post(
                                    "https://ssstik.io/abc?url=dl",
                                    data=post_data,
                                    headers=headers,
                                    proxy=proxy,
                                    timeout=aiohttp.ClientTimeout(total=6.0)
                                ) as r2:
                                    if r2.status == 200:
                                        resp_text = await r2.text()
                except Exception as tte:
                    logger.debug(f"SSSTik dynamic token lookup failed: {tte}")

            if resp_text:
                html_text = resp_text
                # Определяем: это фото-карусель или видео
                is_carousel = ("splide" in html_text or "splide__slide" in html_text or "/photo/" in url)

                all_links = re.findall(r'href=[\'"](https://tikcdn\.io/ssstik/[^\s\'"]+)[\'"]', html_text)
                if not all_links:
                    all_links = re.findall(r'href=[\'"](https://[^\s\'"]+)[\'"][^>]*class=[\'"][^\'"]*download[^\'"]*', html_text)
                
                raw_media_links = [l for l in all_links if "/m/" not in l]
                music_links = [l for l in all_links if "/m/" in l]
                music_url = music_links[0] if music_links else None

                title_match = re.search(r'<p[^>]*class=[\'"][^\'"]*maintext[^\'"]*[\'"][^>]*>(.*?)</p>', html_text, re.DOTALL)
                post_title = ""
                if title_match:
                    raw_t = re.sub(r'<[^>]+>', ' ', title_match.group(1))
                    post_title = py_html.unescape(raw_t).strip()
                    post_title = re.sub(r'\s+', ' ', post_title)
                if not post_title:
                    post_title = "TikTok"

                # 1. Сценарий: Фото-карусель
                if is_carousel and raw_media_links:
                    return {
                        "success": True,
                        "platform": "tiktok",
                        "is_photo": True,
                        "is_video": False,
                        "is_album": len(raw_media_links) > 1,
                        "photo_urls": raw_media_links,
                        "music": music_url,
                        "music_info": None,
                        "title": post_title or "TikTok Слайдшоу",
                        "author": "TikTok",
                        "cover": raw_media_links[0],
                        "digg_count": 0,
                        "comment_count": 0,
                        "share_count": 0
                    }

                # 2. Сценарий: Обычное видео
                if raw_media_links:
                    return {
                        "success": True,
                        "platform": "tiktok",
                        "is_video": True,
                        "is_photo": False,
                        "video_url": raw_media_links[0],
                        "music": music_url,
                        "music_info": None,
                        "title": post_title or "TikTok Video",
                        "author": "TikTok",
                        "cover": "",
                        "duration": 0,
                        "size": 0,
                        "digg_count": 0,
                        "comment_count": 0,
                        "share_count": 0
                    }
    except Exception as err:
        logger.debug(f"SSSTik parse error: {err}")
    return None


async def _parse_tikmate(url: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Резервный парсер через Tikmate API (без водяных знаков)"""
    try:
        session = await http_pool.get_http_session()
        async with session.post(
            "https://api.tikmate.app/api/lookup",
            data={"url": url},
            headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]},
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=4)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                token = data.get("token")
                vid_id = data.get("id")
                if token and vid_id:
                    dl_url = f"https://tikmate.app/download/{token}/{vid_id}.mp4"
                    return {
                        "success": True,
                        "platform": "tiktok",
                        "is_video": True,
                        "video_url": dl_url,
                        "title": data.get("author_name") or "TikTok Video",
                        "author": data.get("author_id") or "TikTok",
                        "cover": "",
                        "duration": 0,
                        "size": 0
                    }
    except Exception as e:
        logger.debug(f"Tikmate parser error: {e}")
    return None


async def _resolve_tiktok_shortlink(url: str, proxy: Optional[str] = None) -> str:
    """
    Разворачивает короткие ссылки vm.tiktok.com / vt.tiktok.com / /t/ в полный URL,
    чтобы сразу определить /photo/ или /video/ и передать в парсеры канонический адрес.
    """
    if not any(d in url for d in ("vm.tiktok.com", "vt.tiktok.com", "/t/")):
        return url
    try:
        headers = {
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        attempts = [None]
        if proxy and proxy not in attempts:
            attempts.append(proxy)
        for p in attempts:
            try:
                session = await http_pool.get_http_session()
                async with session.get(
                    url,
                    allow_redirects=True,
                    headers=headers,
                    proxy=p,
                    timeout=aiohttp.ClientTimeout(total=4.0)
                ) as resp:
                    target = str(resp.url)
                    # ВАЖНО: Развернутый URL валиден только если он указывает на конкретный пост (@user/video/... или @user/photo/...)
                    # Если TikTok перенаправил на главную страницу или логин (tiktok.com/ или tiktok.com/?_r=1) — НЕ используем его!
                    if target and any(k in target for k in ("/video/", "/photo/", "/v/")):
                        clean = target.split("?")[0]
                        logger.info(f"🔗 Развернута короткая ссылка: {url} -> {clean}")
                        return clean
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Shortlink resolve error: {e}")
    return url


async def parse_tiktok(url: str, proxy: Optional[str] = None, warp_proxy_module = None) -> Optional[Dict[str, Any]]:
    """
    Быстрый и надежный парсер TikTok через TikWM API, SSSTik и Tikmate.
    Сначала всегда пробует прямое соединение (самое быстрое <0.5 сек), затем WARP-прокси.
    """
    # 0. Если это короткая ссылка — сначала быстро разворачиваем её в полный URL
    resolved_url = await _resolve_tiktok_shortlink(url, proxy=proxy)

    # Очищаем трекинговые параметры (_r, u_code, sharer_language), которые ломают парсинг в облаке
    target_url = resolved_url.split("?")[0] if ("/photo/" in resolved_url or "/video/" in resolved_url) else resolved_url

    # Формируем цепочку стратегий: прямой доступ ПЕРВЫМ (мгновенно), затем уже поднятый WARP / proxy (не ждём старта WARP)
    strategies = [("direct", None)]
    existing_warp = warp_proxy_module.warp.proxy_url if (warp_proxy_module and hasattr(warp_proxy_module, "warp")) else None
    if existing_warp:
        strategies.append(("warp", existing_warp))
    if proxy and ("proxy", proxy) not in strategies and ("warp", proxy) not in strategies:
        strategies.append(("proxy", proxy))

    is_photo_post = "/photo/" in url or "/photo/" in target_url or "/photo/" in resolved_url

    for strat_name, strat_proxy in strategies:
        # 1. Пробуем TikWM (быстрый таймаут 2.5с, чтобы при сбое не задерживать переход на SSSTik)
        urls_to_try = [target_url]
        if target_url != url:
            urls_to_try.append(url)

        for u_candidate in urls_to_try:
            for endpoint in TIKWM_ENDPOINTS[:1]:
                try:
                    session = await http_pool.get_http_session()
                    async with session.post(
                        endpoint,
                        data={"url": u_candidate, "hd": "1"},
                        headers=DEFAULT_HEADERS,
                        proxy=strat_proxy,
                        timeout=aiohttp.ClientTimeout(total=2.5)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == 0 and data.get("data"):
                                d = data["data"]
                                digg = d.get("digg_count", 0)
                                comm = d.get("comment_count", 0)
                                shares = d.get("share_count", 0)
                                music_u = d.get("music")
                                music_inf = d.get("music_info")

                                images = d.get("images") or []
                                if images:
                                    logger.info(f"✅ [TikTok] Карусель ({len(images)} фото) получена через TikWM ({strat_name})")
                                    return {
                                        "success": True,
                                        "platform": "tiktok",
                                        "is_photo": True,
                                        "is_album": len(images) > 1,
                                        "photo_urls": images,
                                        "music": music_u,
                                        "music_info": music_inf,
                                        "title": d.get("title", "TikTok Фото") or "TikTok Фото",
                                        "author": d.get("author", {}).get("nickname", "TikTok"),
                                        "cover": d.get("cover", ""),
                                        "digg_count": digg,
                                        "comment_count": comm,
                                        "share_count": shares
                                    }

                                video_url = d.get("play") or d.get("wmplay")
                                if video_url:
                                    if video_url.startswith("/"):
                                        video_url = "https://www.tikwm.com" + video_url
                                    logger.info(f"✅ [TikTok] Видео получено через TikWM ({strat_name})")
                                    return {
                                        "success": True,
                                        "platform": "tiktok",
                                        "is_video": True,
                                        "video_url": video_url,
                                        "title": d.get("title", "TikTok Video") or "TikTok Video",
                                        "author": d.get("author", {}).get("nickname", "TikTok"),
                                        "cover": d.get("cover", ""),
                                        "duration": d.get("duration", 0),
                                        "size": d.get("size", 0),
                                        "digg_count": digg,
                                        "comment_count": comm,
                                        "share_count": shares,
                                        "music": music_u,
                                        "music_info": music_inf
                                    }
                except Exception as e:
                    logger.debug(f"TikTok endpoint {endpoint} ({strat_name}) error: {e}")
                    continue

        # 2. Резервный парсер SSSTik (работает и для фото-каруселей, и для видео)
        try:
            ss_fast = await _parse_ssstik(target_url, proxy=strat_proxy)
            if ss_fast:
                if is_photo_post and ss_fast.get("photo_urls"):
                    logger.info(f"✅ [TikTok] Фотопост успешно распарсен через SSSTik ({strat_name})")
                    return ss_fast
                elif not is_photo_post and (ss_fast.get("video_url") or ss_fast.get("is_video")):
                    logger.info(f"✅ [TikTok] Видео успешно получено через SSSTik ({strat_name})")
                    return ss_fast
                elif ss_fast.get("photo_urls") or ss_fast.get("video_url"):
                    return ss_fast
        except Exception as e:
            logger.debug(f"SSSTik ({strat_name}) parse error: {e}")

        # 3. Резервный парсер Tikmate (для видео)
        try:
            tm_res = await _parse_tikmate(target_url, proxy=strat_proxy)
            if tm_res:
                logger.info(f"✅ [TikTok] Видео получено через Tikmate ({strat_name})")
                return tm_res
        except Exception as e:
            logger.debug(f"Tikmate ({strat_name}) error: {e}")

    # 4. Финальный фоллбэк на SSSTik и Tikmate
    for candidate_u in (target_url, url):
        try:
            ss_res = await _parse_ssstik(candidate_u)
            if ss_res:
                return ss_res
        except Exception:
            pass
        try:
            tm_res = await _parse_tikmate(candidate_u)
            if tm_res:
                return tm_res
        except Exception:
            pass

    return None


async def download_file_stream(
    url: str,
    output_path: str,
    max_size_bytes: int = 50 * 1024 * 1024,
    proxy: Optional[str] = None,
    progress_callback = None
) -> str:
    """
    Высокоскоростная потоковая загрузка файла с буфером 1 МБ, поддержкой прокси/WARP и прогресс-бара.
    """
    import time
    ref = "https://ssstik.io/" if ("tikcdn.io" in url or "ssstik" in url) else (
        "https://tikmate.app/" if "tikmate.app" in url else "https://www.tiktok.com/"
    )
    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": ref,
    }

    # Пробуем скачать напрямую (мгновенно), при неудаче пробуем через прокси (если задан)
    attempts = [None]
    if proxy and proxy not in attempts:
        attempts.append(proxy)

    last_err = None
    for cur_proxy in attempts:
        try:
            session = await http_pool.get_http_session()
            async with session.get(url, headers=headers, proxy=cur_proxy, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status} while downloading TikTok stream")

                ct = (resp.headers.get("Content-Type") or "").lower()
                if any(bad in ct for bad in ("text/html", "application/json", "text/xml", "application/xml")):
                    raise Exception(f"Unexpected Content-Type {ct} from TikTok stream")

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
                            speed = downloaded / elapsed if elapsed > 0.05 else 0
                            eta = (total - downloaded) / speed if (speed > 0 and total > downloaded) else 0
                            try:
                                cb_res = progress_callback(downloaded, total, speed, eta)
                                if asyncio.iscoroutine(cb_res):
                                    await cb_res
                            except Exception:
                                pass
            return output_path
        except Exception as e:
            last_err = e
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            continue

    if last_err:
        raise last_err
    return output_path


async def download_tiktok(
    clean_url: str,
    output_prefix: Optional[str] = None,
    download_locally: bool = True,
    ffmpeg_path: str = None,
    warp_proxy_module = None,
    progress_callback = None
) -> Dict[str, Any]:
    """
    Полностью независимый конвейер загрузки TikTok:
    1. Мгновенно извлекает прямой CDN URL без водяных знаков (через WARP или напрямую).
    2. Если download_locally=True, быстро скачивает файл локально с увеличенным буфером.
    3. Поддерживает фото-карусели и наложение музыки на статичные слайды.
    """
    if not output_prefix:
        output_prefix = os.path.join(tempfile.gettempdir(), f"tt_{uuid.uuid4().hex[:8]}")

    # TikTok работает напрямую; используем WARP только если он уже поднят (никогда не блокируемся на его запуске)
    proxy_url = warp_proxy_module.warp.proxy_url if (warp_proxy_module and hasattr(warp_proxy_module, "warp")) else None

    tikwm_data = await parse_tiktok(clean_url, proxy=proxy_url, warp_proxy_module=warp_proxy_module)
    if not tikwm_data:
        # Резервный fallback на yt-dlp, если внешние API не ответили
        try:
            import downloader
            res = await asyncio.to_thread(downloader._download_ytdlp_sync, clean_url, output_prefix, 720, platform="tiktok", proxy_url=proxy_url)
            if res and res.get("file_path"):
                res["platform"] = "tiktok"
                return res
        except Exception as yt_err:
            logger.warning(f"TikTok yt-dlp fallback error: {yt_err}")
        raise Exception("tiktok_parse_failed: не удалось извлечь данные ролика")

    # Сценарий 1: Обычное видео MP4
    if tikwm_data.get("video_url") or tikwm_data.get("is_video"):
        video_url = tikwm_data.get("video_url")
        dest_file = f"{output_prefix}.mp4"
        filesize = tikwm_data.get("size", 0)

        if download_locally:
            downloaded_ok = False
            last_dl_err = None

            if video_url:
                try:
                    await download_file_stream(video_url, dest_file, proxy=proxy_url, progress_callback=progress_callback)
                    if not is_valid_mp4_file(dest_file, allow_audio=False):
                        raise Exception("invalid_mp4_stream")
                    downloaded_ok = True
                except Exception as dl_err:
                    last_dl_err = dl_err
                    logger.warning(f"TikTok local stream download failed ({video_url[:80]}): {dl_err}")
                    if os.path.exists(dest_file):
                        try:
                            os.remove(dest_file)
                        except OSError:
                            pass

            if not downloaded_ok:
                v_lower = (video_url or "").lower()
                alt_parsers = []
                if "tikcdn.io" not in v_lower and "ssstik" not in v_lower:
                    alt_parsers.append(("SSSTik", _parse_ssstik))
                if "tikmate" not in v_lower:
                    alt_parsers.append(("Tikmate", _parse_tikmate))

                for p_name, p_fn in alt_parsers:
                    try:
                        alt_res = await p_fn(clean_url, proxy=proxy_url)
                        alt_url = alt_res.get("video_url") if alt_res else None
                        if not alt_url:
                            continue
                        await download_file_stream(alt_url, dest_file, proxy=proxy_url, progress_callback=progress_callback)
                        if is_valid_mp4_file(dest_file, allow_audio=False):
                            video_url = alt_url
                            downloaded_ok = True
                            logger.info(f"✅ [TikTok] Видео восстановлено через fallback {p_name}")
                            break
                        if os.path.exists(dest_file):
                            os.remove(dest_file)
                    except Exception as alt_err:
                        last_dl_err = alt_err
                        logger.debug(f"TikTok fallback {p_name} failed: {alt_err}")
                        if os.path.exists(dest_file):
                            try:
                                os.remove(dest_file)
                            except OSError:
                                pass

            if not downloaded_ok:
                try:
                    import downloader
                    yt_res = await asyncio.to_thread(
                        downloader._download_ytdlp_sync,
                        clean_url,
                        output_prefix,
                        720,
                        platform="tiktok",
                        proxy_url=proxy_url,
                    )
                    if yt_res and yt_res.get("file_path") and is_valid_mp4_file(yt_res["file_path"], allow_audio=False):
                        yt_res["platform"] = "tiktok"
                        logger.info("✅ [TikTok] Видео восстановлено через yt-dlp fallback")
                        return yt_res
                except Exception as yt_fb_err:
                    last_dl_err = yt_fb_err
                    logger.debug(f"TikTok yt-dlp fallback failed: {yt_fb_err}")

            if not downloaded_ok:
                raise last_dl_err or Exception("tiktok_stream_failed")

            filesize = os.path.getsize(dest_file) if os.path.exists(dest_file) else filesize

        return {
            "file_path": dest_file if download_locally else None,
            "direct_url": video_url,
            "is_video": True,
            "is_photo": False,
            "title": tikwm_data.get("title", "TikTok"),
            "author": tikwm_data.get("author", "TikTok"),
            "duration": tikwm_data.get("duration", 0),
            "platform": "tiktok",
            "cover": tikwm_data.get("cover", ""),
            "filesize": filesize,
            "digg_count": tikwm_data.get("digg_count", 0),
            "comment_count": tikwm_data.get("comment_count", 0),
            "share_count": tikwm_data.get("share_count", 0),
            "music": tikwm_data.get("music"),
            "music_info": tikwm_data.get("music_info"),
            "original_url": clean_url
        }

    # Сценарий 2: Фото-слайдшоу (карусель)
    if tikwm_data.get("is_photo") and tikwm_data.get("photo_urls"):
        photo_urls = tikwm_data["photo_urls"]
        digg_cnt = tikwm_data.get("digg_count", 0)
        comm_cnt = tikwm_data.get("comment_count", 0)
        share_cnt = tikwm_data.get("share_count", 0)
        music_url = tikwm_data.get("music")
        music_inf = tikwm_data.get("music_info")

        if len(photo_urls) > 1:
            targets = photo_urls[:30]
            expected_paths = [f"{output_prefix}_{i}.jpg" for i in range(len(targets))]
            album_photos = [None] * len(targets)
            total_slides = len(targets)
            downloaded_count = 0
            slide_lock = asyncio.Lock()

            async def _dl_slide(idx: int, img_u: str, notify_progress: bool = False):
                nonlocal downloaded_count
                dest = expected_paths[idx]
                tmp_dest = f"{dest}.part"
                try:
                    try:
                        await download_file_stream(img_u, tmp_dest, proxy=proxy_url)
                    except Exception:
                        pass
                    # Если файл не скачался (например, 403 от cdn) и это ссылка tikcdn.io — пробуем оригинальный декодированный URL
                    if (not os.path.exists(tmp_dest) or os.path.getsize(tmp_dest) < 1000) and "tikcdn.io" in img_u:
                        m_b64 = re.search(r'tikcdn\.io/ssstik/(?:m/)?([A-Za-z0-9_-]+)', img_u)
                        if m_b64:
                            b = m_b64.group(1).replace('-', '+').replace('_', '/')
                            b += '=' * ((4 - len(b) % 4) % 4)
                            try:
                                dec = base64.b64decode(b).decode('utf-8', errors='ignore')
                                if dec.startswith("http"):
                                    await download_file_stream(dec, tmp_dest, proxy=proxy_url)
                            except Exception:
                                pass

                    if os.path.exists(tmp_dest) and os.path.getsize(tmp_dest) > 1000:
                        os.replace(tmp_dest, dest)
                        album_photos[idx] = dest
                    elif os.path.exists(tmp_dest):
                        try:
                            os.remove(tmp_dest)
                        except OSError:
                            pass
                except Exception as dl_err:
                    logger.warning(f"TikTok album slide {idx} download error: {dl_err}")
                finally:
                    async with slide_lock:
                        downloaded_count += 1
                        if notify_progress and progress_callback:
                            try:
                                cb = progress_callback(downloaded_count, total_slides, is_slide=True)
                                if asyncio.iscoroutine(cb):
                                    await cb
                            except Exception:
                                pass

            # Fast-Track: качаем сначала только 1-й слайд (около 1 сек) и сразу отдаём пользователю
            await _dl_slide(0, targets[0], notify_progress=True)

            async def _bg_dl_remaining():
                sem = asyncio.Semaphore(4)
                async def _bounded(i: int, u: str):
                    async with sem:
                        await _dl_slide(i, u, notify_progress=False)
                await asyncio.gather(*[_bounded(i, targets[i]) for i in range(1, len(targets))], return_exceptions=True)

            bg_task = asyncio.create_task(_bg_dl_remaining())

            return {
                "is_album": True,
                "photos": expected_paths,
                "photo_urls": targets,
                "file_path": album_photos[0],
                "fast_track": True,
                "_bg_task": bg_task,
                "platform": "tiktok",
                "title": tikwm_data.get("title", "TikTok Слайдшоу"),
                "author": tikwm_data.get("author", "TikTok"),
                "digg_count": digg_cnt,
                "comment_count": comm_cnt,
                "share_count": share_cnt,
                "music": music_url,
                "music_info": music_inf,
                "original_url": clean_url
            }

        # Одиночный слайд
        dest_file = f"{output_prefix}.jpg"
        await download_file_stream(photo_urls[0], dest_file, proxy=proxy_url)
        if progress_callback:
            try:
                cb = progress_callback(1, 1, is_slide=True)
                if asyncio.iscoroutine(cb):
                    await cb
            except Exception:
                pass

        # Строгая валидация по сигнатурам (magic bytes):
        if is_valid_mp4_file(dest_file, allow_audio=False):
            mp4_file = f"{output_prefix}.mp4"
            try:
                if os.path.exists(mp4_file):
                    os.remove(mp4_file)
                os.rename(dest_file, mp4_file)
                dest_file = mp4_file
            except Exception:
                pass
            return {
                "file_path": dest_file,
                "is_video": True,
                "is_photo": False,
                "title": tikwm_data.get("title", "TikTok Video"),
                "author": tikwm_data.get("author", "TikTok"),
                "platform": "tiktok",
                "filesize": os.path.getsize(dest_file) if os.path.exists(dest_file) else 0,
                "digg_count": digg_cnt,
                "comment_count": comm_cnt,
                "share_count": share_cnt,
                "music": music_url,
                "music_info": music_inf,
                "original_url": clean_url
            }

        return {
            "file_path": dest_file,
            "is_photo": True,
            "is_video": False,
            "title": tikwm_data.get("title", "TikTok Фото"),
            "author": tikwm_data.get("author", "TikTok"),
            "platform": "tiktok",
            "filesize": os.path.getsize(dest_file) if os.path.exists(dest_file) else 0,
            "digg_count": digg_cnt,
            "comment_count": comm_cnt,
            "share_count": share_cnt,
            "music": music_url,
            "music_info": music_inf,
            "original_url": clean_url
        }

    raise Exception("tiktok_unsupported_content")
