import os
import re
import asyncio
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("youtube_service")

# Regex для YouTube (Shorts, Watch, YouTu.be, Live)
YOUTUBE_REGEX = re.compile(
    r'https?://(?:www\.|m\.|music\.)?'
    r'(?:youtube\.com/(?:watch\?[^\s#]*v=|shorts/|live/|embed/|v/)|youtu\.be/)[^\s]+'
)


def extract_youtube_id(url: str) -> Optional[str]:
    m = re.search(
        r'(?:youtu\.be/|youtube\.com/(?:shorts/|live/|watch\?v=|embed/|v/))([A-Za-z0-9_-]{11})',
        url or "",
    )
    return m.group(1) if m else None


async def download_youtube(
    clean_url: str,
    output_prefix: Optional[str] = None,
    ytdlp_sync_func=None,
    warp_proxy_module=None
) -> Dict[str, Any]:
    """
    Полностью независимый конвейер загрузки YouTube:
    - WARP-прокси (usque) подменяет IP дата-центра Render на Cloudflare,
      обходя "Sign in to confirm you're not a bot".
    - visionos-клиент обходит ограничения SABR и PO-token.
    - Никаких cookies и аккаунта Google не требуется.
    - Автоматический каскад качества: 1280 (вертикальный 720p) -> 480 -> 360.
    """
    if not output_prefix:
        import tempfile, uuid
        output_prefix = os.path.join(tempfile.gettempdir(), f"yt_{uuid.uuid4().hex[:8]}")
    is_short = "/shorts/" in clean_url
    heights = [1280, 480, 360] if is_short else [720, 480, 360]

    manual_proxy = (
        os.getenv("YTDLP_PROXY")
        or os.getenv("YOUTUBE_PROXY")
        or os.getenv("PROXY")
        or os.getenv("HTTPS_PROXY")
        or ""
    ).strip()

    strategies = []

    # 1. Основная стратегия: локальный WARP
    warp_ready = False
    if warp_proxy_module and warp_proxy_module.warp.enabled and warp_proxy_module.warp.supported:
        try:
            warp_ready = await warp_proxy_module.warp.ensure_ready()
        except Exception as e:
            logger.warning(f"YouTube WARP init error: {e}")
    if warp_ready and warp_proxy_module.warp.proxy_url:
        strategies.append(("warp", warp_proxy_module.warp.proxy_url))

    # 2. Ручной прокси из окружения (если настроен)
    if manual_proxy:
        strategies.append(("manual-proxy", manual_proxy))

    # 3. Прямой запасной вариант
    strategies.append(("direct", ""))

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
                    ytdlp_sync_func,
                    clean_url,
                    output_prefix,
                    height,
                    extractor_args=visionos_args,
                    platform="youtube",
                    use_cookies=False,
                    proxy_url=strategy_proxy,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"[YouTube] {strategy_name} {height}p failed for {clean_url}: {e}"
                )
                if strategy_name == "warp" and any(
                    token in str(e).lower()
                    for token in (
                        "proxy", "tunnel", "timed out", "connection", "403",
                        "not a bot", "sign in", "login_required",
                    )
                ):
                    try:
                        await warp_proxy_module.warp.mark_unhealthy()
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
                    f"[YouTube] {strategy_name} {height}p: файл {size / 1048576:.1f} МБ > 50 МБ, пробую качество ниже"
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

        # Если на этом качестве упало не из-за превышения размера файла (>50МБ),
        # то переход на 480p/360p не поможет обойти сетевую ошибку.
        if not size_error:
            break

    if size_error and last_error is None:
        raise Exception("file_too_large: видео больше 50 МБ даже в 360p")
    raise Exception(f"youtube_bot_detection: {last_error}")
