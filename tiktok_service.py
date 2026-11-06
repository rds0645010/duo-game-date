import os
import re
import uuid
import tempfile
import asyncio
import logging
import aiohttp
from typing import Optional, Dict, Any, List

logger = logging.getLogger("tiktok_service")

# Regex для распознавания ссылок TikTok
TIKTOK_REGEX = re.compile(r'https?://(?:www\.|v[mt]\.)?tiktok\.com/[^\s]+')

# Зеркала и эндпоинты API TikWM
TIKWM_ENDPOINTS = [
    "https://www.tikwm.com/api/",
    "https://tikwm.com/api/"
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _parse_tikmate(url: str) -> Optional[Dict[str, Any]]:
    """Резервный парсер через Tikmate API (без водяных знаков)"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.tikmate.app/api/lookup",
                data={"url": url},
                headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]},
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


async def parse_tiktok(url: str) -> Optional[Dict[str, Any]]:
    """
    Быстрый парсер TikTok через TikWM API и резервный Tikmate API (без водяных знаков).
    Опрашивает эндпоинты с коротким таймаутом (< 3 сек).
    Возвращает словарь с метаданными и прямой ссылкой на поток.
    """
    target_url = url

    # 1. Сначала пробуем TikWM напрямую с исходным URL (включая vt.tiktok.com/vm.tiktok.com)
    for endpoint in TIKWM_ENDPOINTS:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    data={"url": target_url},
                    headers=DEFAULT_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=3.5)
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

                            video_url = d.get("play") or d.get("wmplay")
                            if video_url:
                                if video_url.startswith("/"):
                                    video_url = "https://www.tikwm.com" + video_url
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
                            images = d.get("images") or []
                            if images:
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
        except Exception as e:
            logger.debug(f"TikTok endpoint {endpoint} error: {e}")
            continue

    # 2. Если прямой запрос не сработал и это короткая ссылка — пробуем развернуть редирект
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as s:
                async with s.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=2.5)) as r:
                    if r.url and "video" in str(r.url):
                        target_url = str(r.url).split("?")[0]
                        # Пробуем TikWM ещё раз с развернутым URL
                        for endpoint in TIKWM_ENDPOINTS:
                            try:
                                async with aiohttp.ClientSession() as session:
                                    async with session.post(
                                        endpoint,
                                        data={"url": target_url},
                                        headers=DEFAULT_HEADERS,
                                        timeout=aiohttp.ClientTimeout(total=3)
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

                                                video_url = d.get("play") or d.get("wmplay")
                                                if video_url:
                                                    if video_url.startswith("/"):
                                                        video_url = "https://www.tikwm.com" + video_url
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
                                                images = d.get("images") or []
                                                if images:
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
                            except Exception:
                                pass
        except Exception:
            pass

    # 3. Резервный парсер Tikmate (если TikWM выдал rate limit или недоступен)
    tikmate_res = await _parse_tikmate(target_url)
    if tikmate_res:
        return tikmate_res

    return None


async def download_file_stream(
    url: str,
    output_path: str,
    max_size_bytes: int = 50 * 1024 * 1024,
    progress_callback = None
) -> str:
    """
    Высокоскоростная потоковая загрузка файла с буфером 1 МБ и поддержкой прогресс-бара.
    """
    import time
    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": "https://www.tiktok.com/",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status} while downloading TikTok stream")

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


async def download_tiktok(
    clean_url: str,
    output_prefix: Optional[str] = None,
    download_locally: bool = True,
    ffmpeg_path: str = None,
    progress_callback = None
) -> Dict[str, Any]:
    """
    Полностью независимый конвейер загрузки TikTok:
    1. Мгновенно извлекает прямой CDN URL без водяных знаков.
    2. Если download_locally=True, быстро скачивает файл локально с увеличенным буфером.
    3. Поддерживает фото-карусели и наложение музыки на статичные слайды.
    """
    if not output_prefix:
        output_prefix = os.path.join(tempfile.gettempdir(), f"tt_{uuid.uuid4().hex[:8]}")
    tikwm_data = await parse_tiktok(clean_url)
    if not tikwm_data:
        # Резервный fallback на yt-dlp, если внешние API не ответили
        try:
            import downloader
            res = await asyncio.to_thread(downloader._download_ytdlp_sync, clean_url, output_prefix, 720, platform="tiktok")
            if res and res.get("file_path"):
                res["platform"] = "tiktok"
                return res
        except Exception as yt_err:
            logger.warning(f"TikTok yt-dlp fallback error: {yt_err}")
        raise Exception("tiktok_parse_failed: не удалось извлечь данные ролика")

    # Сценарий 1: Обычное видео MP4
    if tikwm_data.get("video_url"):
        video_url = tikwm_data["video_url"]
        dest_file = f"{output_prefix}.mp4"
        filesize = tikwm_data.get("size", 0)

        if download_locally:
            try:
                await download_file_stream(video_url, dest_file, progress_callback=progress_callback)
                filesize = os.path.getsize(dest_file) if os.path.exists(dest_file) else filesize
            except Exception as dl_err:
                logger.warning(f"TikTok local stream download failed: {dl_err}")
                if os.path.exists(dest_file):
                    try:
                        os.remove(dest_file)
                    except OSError:
                        pass
                raise dl_err

        return {
            "file_path": dest_file if download_locally else None,
            "direct_url": video_url,
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
            album_photos = [None] * len(targets)

            async def _dl_slide(idx: int, img_u: str):
                dest = f"{output_prefix}_{idx}.jpg"
                try:
                    await download_file_stream(img_u, dest)
                    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
                        album_photos[idx] = dest
                except Exception as dl_err:
                    logger.warning(f"TikTok album slide {idx} download error: {dl_err}")

            await asyncio.gather(*[_dl_slide(i, u) for i, u in enumerate(targets)])
            valid_photos = [p for p in album_photos if p]

            if len(valid_photos) > 1:
                return {
                    "is_album": True,
                    "photos": valid_photos,
                    "file_path": valid_photos[0],
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
        await download_file_stream(photo_urls[0], dest_file)

        # Если есть музыка и ffmpeg — склеиваем в видео MP4
        if music_url and ffmpeg_path and os.path.exists(ffmpeg_path):
            audio_file = f"{output_prefix}.mp3"
            mp4_file = f"{output_prefix}.mp4"
            try:
                await download_file_stream(music_url, audio_file)
                import subprocess
                cmd = [
                    ffmpeg_path, "-y", "-loop", "1", "-i", dest_file,
                    "-i", audio_file, "-c:v", "libx264", "-tune", "stillimage",
                    "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
                    "-shortest", "-movflags", "+faststart", mp4_file
                ]
                proc = await asyncio.to_thread(
                    subprocess.run, cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25
                )
                if proc.returncode == 0 and os.path.exists(mp4_file):
                    for tmp in (audio_file, dest_file):
                        if os.path.exists(tmp):
                            try: os.remove(tmp)
                            except OSError: pass
                    return {
                        "file_path": mp4_file,
                        "platform": "tiktok",
                        "title": tikwm_data.get("title", "TikTok Видео (Фото со звуком)"),
                        "author": tikwm_data.get("author", "TikTok"),
                        "filesize": os.path.getsize(mp4_file),
                        "digg_count": digg_cnt,
                        "comment_count": comm_cnt,
                        "share_count": share_cnt,
                        "music": music_url,
                        "music_info": music_inf,
                        "original_url": clean_url
                    }
            except Exception as m_err:
                logger.warning(f"TikTok music merge error: {m_err}")

        return {
            "file_path": dest_file,
            "is_photo": True,
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
