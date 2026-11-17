"""
Модульный пакет маршрутизаторов (aiogram.Router) и WebApp API.
Разделяет монолит на независимые домены ("пазлы"):
- admin: права доступа, cookies 18+, диагностика, проверка Telegram WebApp initData
- webapp_api: /start, /settings, инвайты DUO и HTTP API Mini App
- slideshow: интерактивные фото/видео карусели и Fast-Track предзагрузка
- music: команда /music и скачивание треков
- inline: инлайн-режим (@бот <ссылка>)
- downloads: скачивание видео и аудио в ЛС + защита от дублей/спама
"""

import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)

_app_module: Optional[Any] = None


def set_app_module(mod: Any):
    """Регистрирует корневой модуль приложения (защита от двойного импорта __main__)."""
    global _app_module
    _app_module = mod


def app() -> Any:
    """Возвращает активный корневой модуль бота."""
    if _app_module is not None:
        return _app_module
    import bot as _bot_mod
    return _bot_mod


from . import admin, webapp_api, slideshow, music, inline, downloads  # noqa: E402

__all__ = [
    "set_app_module",
    "app",
    "setup_all_routers",
    "admin",
    "webapp_api",
    "slideshow",
    "music",
    "inline",
    "downloads",
]


def setup_all_routers(dp) -> None:
    """Подключает все изолированные роутеры к Dispatcher в правильном приоритете."""
    dp.include_router(admin.router)
    dp.include_router(webapp_api.router)
    dp.include_router(slideshow.router)
    dp.include_router(music.router)
    dp.include_router(inline.router)
    # downloads.router содержит catch-all F.text | F.caption — подключается ПОСЛЕДНИМ
    dp.include_router(downloads.router)
    logger.info("🧩 Все модульные роутеры подключены: admin, webapp_api, slideshow, music, inline, downloads")
