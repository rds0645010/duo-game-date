#!/usr/bin/env python3
"""
bootstrap_usque.py — подготовка Cloudflare WARP на этапе сборки.

Render выполняет `python bootstrap_usque.py --register` в buildCommand:
  * скачивает бинарник usque (open-source клиент WARP) в bin/;
  * один раз регистрирует анонимный WARP-конфиг в warp_data/config.json.

Регистрация не требует аккаунта, e-mail, cookies или API-ключей.
Если сеть на этапе сборки недоступна, скрипт не валит деплой: бот
попробует сделать то же самое при первом запросе YouTube.
"""

import argparse
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

import warp_proxy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Подготовка usque/WARP для YouTube")
    parser.add_argument(
        "--register",
        action="store_true",
        help="зарегистрировать анонимный WARP-конфиг, если его ещё нет",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="только скачать бинарник, без регистрации",
    )
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    args = parser.parse_args()

    register = args.register and not args.no_register
    result = warp_proxy.warm_up_sync(register=register)
    result["data_dir"] = warp_proxy._DATA_DIR
    result["config_path"] = warp_proxy.CONFIG_PATH
    result["version"] = warp_proxy.USQUE_VERSION

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result.get("skipped"):
            print(f"ℹ️ WARP пропущен: {result['skipped']}")
        elif result["ok"]:
            print(f"✅ usque готов: {result['binary']}")
            if result.get("config"):
                print(f"✅ WARP-конфиг готов: {result['config']}")
        else:
            print(f"⚠️ WARP не подготовлен: {result['error']}")
            print("   Бот попробует повторить это при первом запросе YouTube.")

    # Никогда не валим деплой из-за WARP: у бота есть прямой fallback.
    return 0


if __name__ == "__main__":
    sys.exit(main())
