"""
warp_proxy.py — автоматический Cloudflare WARP (MASQUE) прокси для YouTube.

Зачем это нужно
---------------
YouTube блокирует IP-адреса дата-центров (Render, AWS, GCP и т.д.) ошибкой
"Sign in to confirm you're not a bot". Cookies и PO-token'ы помогают не всегда
и требуют аккаунта Google. Cloudflare WARP подменяет исходящий IP на IP
Cloudflare, который YouTube не считает заблокированным.

Как это работает
----------------
usque — открытая реализация WARP по протоколу MASQUE. Режим
`http-proxy --http2` ходит через обычный TCP/443, поэтому работает даже там,
где UDP закрыт. Регистрация в WARP анонимная и выполняется автоматически:
пользователю не нужно нигде регистрироваться и вставлять cookies.

Ресурсы
-------
~20-30 МБ RAM на Linux. Прокси поднимается только когда бот скачивает видео
с YouTube; TikTok и Instagram идут напрямую и WARP не используют.

Отключить: WARP_ENABLED=false в переменных окружения.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import stat
import subprocess
import time
import urllib.request
import zipfile

logger = logging.getLogger(__name__)

# ── Версия и контрольные суммы usque (open-source, MIT) ───────────────────────
USQUE_VERSION = "4.2.1"
_RELEASE_URL = (
    f"https://github.com/Diniboy1123/usque/releases/download/v{USQUE_VERSION}"
)

# platform_key -> (имя файла, sha256)
_USQUE_ASSETS = {
    "linux_amd64": (
        f"usque_{USQUE_VERSION}_linux_amd64.zip",
        "4117e20695078af9c11edecd1a826c009bbc7ea0b7f64458612b4198910bc313",
    ),
    "linux_arm64": (
        f"usque_{USQUE_VERSION}_linux_arm64.zip",
        "c88b061c2a567f30813d7505637d1fb6fe7ec5898b5b61fd05122409fa5ad925",
    ),
    "windows_amd64": (
        f"usque_{USQUE_VERSION}_windows_amd64.zip",
        "f6f7f0a1a2bc9bcc15cf563ec1f892d00690a92c086b23ed3211b802209099e7",
    ),
    "darwin_amd64": (
        f"usque_{USQUE_VERSION}_darwin_amd64.zip",
        "1eb41e34bf4cd0b81e06222ef844cea75eb56229a62fe29c07cf8e7bd253357d",
    ),
    "darwin_arm64": (
        f"usque_{USQUE_VERSION}_darwin_arm64.zip",
        "762e2dc875669566207a3c776a53dc6bb50770da25f90e1ab69fbc53e91f8da1",
    ),
}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.abspath(os.getenv("WARP_BIN_DIR") or os.path.join(_BASE_DIR, "bin"))
_DATA_DIR = os.path.abspath(
    os.path.expanduser(os.getenv("WARP_DATA_DIR") or os.path.join(_BASE_DIR, "warp_data"))
)
CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")
LOG_PATH = os.path.join(_DATA_DIR, "usque.log")
_WARP_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def _platform_key() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        os_name = "linux"
    elif system == "windows":
        os_name = "windows"
    elif system == "darwin":
        os_name = "darwin"
    else:
        return None
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not arch:
        return None
    return f"{os_name}_{arch}"


def _binary_name() -> str:
    return "usque.exe" if os.name == "nt" else "usque"


def find_binary() -> str | None:
    """Возвращает путь к уже установленному usque, если он есть."""
    env_bin = os.getenv("USQUE_BIN_PATH", "").strip()
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    candidate = os.path.join(_BIN_DIR, _binary_name())
    if os.path.isfile(candidate):
        return candidate
    which = shutil.which("usque")
    if which:
        return which
    return None


def _download_binary() -> str:
    """Скачивает usque с GitHub, проверяет SHA256 и распаковывает в bin/."""
    key = _platform_key()
    if not key or key not in _USQUE_ASSETS:
        raise RuntimeError(f"Платформа {platform.system()} {platform.machine()} не поддерживается WARP")
    asset_name, expected_sha = _USQUE_ASSETS[key]
    url = f"{_RELEASE_URL}/{asset_name}"

    os.makedirs(_BIN_DIR, exist_ok=True)
    dest = os.path.join(_BIN_DIR, _binary_name())
    tmp_zip = os.path.join(_BIN_DIR, ".usque_download.zip")
    tmp_bin = dest + ".tmp"

    logger.info("Скачиваю usque %s (%s)...", USQUE_VERSION, asset_name)
    req = urllib.request.Request(url, headers={"User-Agent": "duo-bot/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp_zip, "wb") as f:
        shutil.copyfileobj(resp, f)

    sha = hashlib.sha256()
    with open(tmp_zip, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    actual_sha = sha.hexdigest()
    if actual_sha.lower() != expected_sha.lower():
        try:
            os.remove(tmp_zip)
        except OSError:
            pass
        raise RuntimeError(f"Контрольная сумма usque не совпала: {actual_sha}")

    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            chosen = None
            for m in members:
                if os.path.basename(m.filename).lower() in ("usque", "usque.exe"):
                    chosen = m
                    break
            if chosen is None:
                chosen = members[0]
            with zf.open(chosen) as src, open(tmp_bin, "wb") as out:
                shutil.copyfileobj(src, out)
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass

    # chmod +x (на Windows .exe не требует, но вызов безопасен)
    try:
        os.chmod(tmp_bin, os.stat(tmp_bin).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    os.replace(tmp_bin, dest)
    logger.info("usque установлен: %s (%d КБ)", dest, os.path.getsize(dest) // 1024)
    return dest


def ensure_binary_sync() -> str:
    """Гарантирует наличие бинарника usque. Возвращает путь."""
    existing = find_binary()
    if existing and os.path.getsize(existing) > 1024 * 1024:
        return existing
    return _download_binary()


def _config_is_valid(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return all(str(data.get(k) or "").strip() for k in ("private_key", "id", "access_token", "ipv4"))
    except Exception:
        return False


def ensure_config_sync(binary: str | None = None) -> str:
    """
    Гарантирует наличие config.json WARP. Если его нет — регистрирует
    анонимный WARP-девайс через `usque register -a` (без интерактива).
    """
    if os.path.isfile(CONFIG_PATH) and _config_is_valid(CONFIG_PATH):
        return CONFIG_PATH

    binary = binary or ensure_binary_sync()
    os.makedirs(_DATA_DIR, exist_ok=True)
    try:
        os.chmod(_DATA_DIR, 0o700)
    except OSError:
        pass

    # Битый/старый конфиг убираем, чтобы регистрация создала новый
    if os.path.exists(CONFIG_PATH):
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".broken")
        except OSError:
            try:
                os.remove(CONFIG_PATH)
            except OSError:
                pass

    logger.info("Регистрирую анонимный Cloudflare WARP (usque register -a)...")
    proc = subprocess.run(
        [binary, "-c", CONFIG_PATH, "register", "-a"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        text=True,
        errors="replace",
    )
    output = (proc.stdout or "").strip()
    if proc.returncode != 0 or not os.path.isfile(CONFIG_PATH):
        tail = "\n".join(output.splitlines()[-6:])
        raise RuntimeError(f"usque register завершился с кодом {proc.returncode}: {tail}")

    if not _config_is_valid(CONFIG_PATH):
        raise RuntimeError("usque register создал некорректный config.json")

    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    logger.info("WARP-конфиг создан: %s", CONFIG_PATH)
    return CONFIG_PATH


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _read_rss_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        if os.path.isdir("/proc"):
            with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return round(int(line.split()[1]) / 1024, 1)
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            # "usque.exe","1234","Console","1","22 344 КБ"
            for part in out.replace('"', "").split(","):
                part = part.strip().replace(" ", "").replace("\xa0", "")
                if part.endswith("КБ") or part.endswith("K"):
                    digits = "".join(ch for ch in part if ch.isdigit())
                    if digits:
                        return round(int(digits) / 1024, 1)
    except Exception:
        pass
    return None


class WarpProxy:
    """Один локальный HTTP-прокси WARP на процесс бота."""

    def __init__(self) -> None:
        self.enabled = os.getenv("WARP_ENABLED", "true").strip().lower() not in (
            "0", "false", "no", "off", "disabled",
        )
        self.supported = _platform_key() in _USQUE_ASSETS
        self._lock: asyncio.Lock | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._ready = False
        self._egress_ip: str | None = None
        self._colo: str | None = None
        self._last_error = ""
        self._started_at: float | None = None
        self._next_retry_at = 0.0
        self._binary: str | None = None
        self._log_file = None
        atexit.register(self.stop)

    # ── публичное состояние ──────────────────────────────────────────────────
    @property
    def proxy_url(self) -> str | None:
        if self._ready and self._port:
            return f"http://127.0.0.1:{self._port}"
        return None

    def get_proxy_url(self) -> str | None:
        return self.proxy_url

    def _is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict:
        binary = self._binary or find_binary()
        return {
            "enabled": self.enabled,
            "supported": self.supported,
            "ready": bool(self._ready and self._is_running()),
            "proxy": self.proxy_url,
            "port": self._port,
            "pid": self._proc.pid if self._proc else None,
            "egress_ip": self._egress_ip,
            "egress_colo": self._colo,
            "version": USQUE_VERSION,
            "binary": bool(binary and os.path.isfile(binary)),
            "config": bool(os.path.isfile(CONFIG_PATH) and _config_is_valid(CONFIG_PATH)),
            "rss_mb": _read_rss_mb(self._proc.pid if self._proc else None),
            "started_at": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._started_at))
                if self._started_at else None
            ),
            "last_error": self._last_error[:300],
        }

    # ── запуск / остановка ───────────────────────────────────────────────────
    async def ensure_ready(self, timeout: float = 75.0) -> bool:
        """Поднимает WARP при первом обращении. Возвращает True, если прокси готов."""
        if not self.enabled or not self.supported:
            return False
        if self._ready and self._is_running():
            return True
        if time.time() < self._next_retry_at:
            return False

        if self._lock is None:
            self._lock = asyncio.Lock()

        try:
            return await asyncio.wait_for(self._ensure_ready_locked(), timeout=timeout)
        except asyncio.TimeoutError:
            self._last_error = f"WARP не запустился за {timeout:.0f} сек"
            logger.warning(self._last_error)
            self._next_retry_at = time.time() + 120
            await self._kill()
            return False
        except Exception as e:
            self._last_error = str(e)
            logger.warning("WARP: %s", e)
            self._next_retry_at = time.time() + 120
            await self._kill()
            return False

    async def _ensure_ready_locked(self) -> bool:
        async with self._lock:
            if self._ready and self._is_running():
                return True
            if self._is_running():
                # Процесс жив, но не подтвердил готовность — проверим порт
                # и реальный выход в WARP (warp=on), а не просто listen().
                if (
                    self._port
                    and await self._port_open(self._port, 3.0)
                    and await self._check_egress()
                ):
                    self._ready = True
                    return True
                await self._kill()

            binary, config = await asyncio.to_thread(self._prepare_sync)
            port = int(os.getenv("WARP_PROXY_PORT", "0") or 0) or _find_free_port()
            os.makedirs(_DATA_DIR, exist_ok=True)
            try:
                self._log_file = open(LOG_PATH, "wb", buffering=0)
            except OSError:
                self._log_file = None

            cmd = [
                binary, "-c", config, "http-proxy", "--http2",
                "-l", "--system-dns",
                "-b", "127.0.0.1", "-p", str(port),
            ]
            logger.info("Запускаю WARP-прокси: порт %s", port)
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=self._log_file or asyncio.subprocess.DEVNULL,
                stderr=self._log_file or asyncio.subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
            self._port = port

            if not await self._port_open(port, 20.0):
                raise RuntimeError(self._startup_error("WARP-прокси не открыл порт"))

            # Первый запрос через прокси заодно поднимает MASQUE-туннель.
            if not await self._check_egress():
                await asyncio.sleep(2.0)
                if not await self._check_egress():
                    raise RuntimeError(self._startup_error("WARP не подтвердил warp=on"))

            self._ready = True
            self._started_at = time.time()
            self._next_retry_at = 0.0
            logger.info(
                "✅ WARP готов: %s, IP %s (%s), RAM ~%s МБ",
                self.proxy_url, self._egress_ip, self._colo,
                _read_rss_mb(self._proc.pid) if self._proc else "?",
            )
            return True

    def _prepare_sync(self) -> tuple[str, str]:
        binary = ensure_binary_sync()
        self._binary = binary
        config = ensure_config_sync(binary)
        return binary, config

    def _startup_error(self, prefix: str) -> str:
        tail = ""
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-6:]
            tail = " | ".join(line.strip() for line in lines if line.strip())
        except Exception:
            pass
        code = self._proc.returncode if self._proc else None
        return f"{prefix} (код {code}): {tail[-400:]}"

    async def _port_open(self, port: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                return False
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return True
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.25)
        return False

    async def _check_egress(self) -> bool:
        import aiohttp

        # Важно: на этапе проверки self._ready ещё False, поэтому нельзя
        # использовать self.proxy_url (он вернёт None и запрос уйдёт напрямую).
        proxy = f"http://127.0.0.1:{self._port}" if self._port else self.proxy_url
        if not proxy:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _WARP_TRACE_URL,
                    proxy=proxy,
                    timeout=aiohttp.ClientTimeout(total=25),
                    headers={"User-Agent": "duo-bot/1.0"},
                ) as resp:
                    text = await resp.text()
            data = {}
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
            if data.get("warp") == "on" and data.get("ip"):
                self._egress_ip = data["ip"]
                self._colo = data.get("colo")
                return True
            logger.warning("WARP trace без warp=on: %s", text[:200].replace("\n", " | "))
        except Exception as e:
            logger.debug("WARP trace error: %s", e)
        return False

    async def mark_unhealthy(self) -> None:
        """Сбрасывает зависший туннель, чтобы следующий запрос поднял его заново."""
        self._ready = False
        self._next_retry_at = 0.0
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._kill()

    async def _kill(self) -> None:
        self._ready = False
        self._port = None
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:
                    pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def stop(self) -> None:
        """Синхронная остановка (atexit / SIGTERM)."""
        proc = self._proc
        self._proc = None
        self._ready = False
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                # asyncio.subprocess.Process.wait() — корутина, поэтому в
                # синхронном atexit просто ждём returncode.
                for _ in range(6):
                    if proc.returncode is not None:
                        break
                    time.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
            except Exception:
                pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None


# Глобальный экземпляр: импортируется из downloader.py и bot.py
warp = WarpProxy()


def warm_up_sync(register: bool = True) -> dict:
    """
    Подготовка на этапе сборки (bootstrap_usque.py): скачать usque и,
    при необходимости, один раз зарегистрировать WARP-конфиг.
    Не запускает прокси и не требует event loop.
    """
    result = {"ok": False, "binary": None, "config": None, "error": None, "skipped": None}
    if not warp.enabled:
        result["ok"] = True
        result["skipped"] = "WARP_ENABLED=false"
        return result
    if not warp.supported:
        result["ok"] = True
        result["skipped"] = f"платформа {platform.system()} {platform.machine()} не поддерживается"
        return result
    try:
        binary = ensure_binary_sync()
        result["binary"] = binary
        if register:
            result["config"] = ensure_config_sync(binary)
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
        logger.warning("WARP bootstrap: %s", e)
    return result
