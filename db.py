"""
Изолированный слой работы с SQLite (WAL-режим, контекстный менеджер соединений,
асинхронная обёртка run_db и автоочистка устаревших токенов/сессий/кэша).
"""

import re
import json
import time
import uuid
import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any, List, Callable, Tuple

logger = logging.getLogger(__name__)

DB_PATH = "users.db"


@contextmanager
def get_db_connection(db_path: str = None):
    """Открывает соединение SQLite с busy_timeout=5000 и нормальной синхронизацией."""
    conn = sqlite3.connect(db_path or DB_PATH, timeout=5.0)
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


async def run_db(func: Callable, *args, **kwargs):
    """Выполняет синхронную операцию БД в thread pool, не блокируя event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def init_db(db_path: str = None):
    """Инициализирует таблицы БД, индексы и включает SQLite WAL-режим."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                photo_url TEXT,
                gender TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER,
                sender_name TEXT,
                recipient_username TEXT,
                place TEXT,
                day TEXT,
                time TEXT,
                vibe TEXT,
                duration TEXT,
                note TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS video_cache (
                url TEXT PRIMARY KEY,
                file_id TEXT,
                media_type TEXT DEFAULT 'video',
                title TEXT,
                platform TEXT,
                author TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for ddl in (
            "ALTER TABLE video_cache ADD COLUMN media_type TEXT DEFAULT 'video'",
            "ALTER TABLE video_cache ADD COLUMN music TEXT",
            "ALTER TABLE video_cache ADD COLUMN music_info_json TEXT",
        ):
            try:
                cursor.execute(ddl)
            except Exception:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dl_tokens (
                token TEXT PRIMARY KEY,
                url TEXT,
                file_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("ALTER TABLE dl_tokens ADD COLUMN file_path TEXT")
        except Exception:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                url TEXT,
                canonical_key TEXT,
                file_id TEXT,
                media_type TEXT DEFAULT 'video',
                title TEXT,
                platform TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_downloads_user ON user_downloads(user_id, created_at DESC)"
            )
        except Exception:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                inline_caption INTEGER DEFAULT 0,
                save_inline_to_pm INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN save_inline_to_pm INTEGER DEFAULT 0")
        except Exception:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS slideshow_sessions (
                sid TEXT PRIMARY KEY,
                url TEXT,
                platform TEXT,
                title TEXT,
                author TEXT,
                total INTEGER,
                file_ids_json TEXT,
                photo_urls_json TEXT,
                music TEXT,
                music_info_json TEXT,
                chat_id INTEGER,
                created_at REAL,
                digg_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0
            )
        """)
        for ddl in (
            "ALTER TABLE slideshow_sessions ADD COLUMN digg_count INTEGER DEFAULT 0",
            "ALTER TABLE slideshow_sessions ADD COLUMN comment_count INTEGER DEFAULT 0",
        ):
            try:
                cursor.execute(ddl)
            except Exception:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def cleanup_old_db_records(
    db_path: str = None,
    max_token_age_sec: int = 7 * 86400,
    max_slide_age_sec: int = 14 * 86400,
    max_cache_age_sec: int = 60 * 86400,
) -> Dict[str, int]:
    """Удаляет устаревшие токены, старые сессии слайдшоу и протухшие записи кэша."""
    counts = {"dl_tokens": 0, "slideshow_sessions": 0, "video_cache": 0}
    try:
        with get_db_connection(db_path) as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "DELETE FROM dl_tokens WHERE created_at IS NOT NULL AND created_at < datetime('now', ?)",
                    (f"-{int(max_token_age_sec)} seconds",),
                )
                counts["dl_tokens"] = max(0, cur.rowcount or 0)
            except Exception as e:
                logger.debug(f"cleanup dl_tokens notice: {e}")

            try:
                cutoff = time.time() - float(max_slide_age_sec)
                cur.execute(
                    "DELETE FROM slideshow_sessions WHERE created_at IS NOT NULL AND created_at < ?",
                    (cutoff,),
                )
                counts["slideshow_sessions"] = max(0, cur.rowcount or 0)
            except Exception as e:
                logger.debug(f"cleanup slideshow_sessions notice: {e}")

            try:
                cur.execute(
                    "DELETE FROM video_cache WHERE created_at IS NOT NULL AND created_at < datetime('now', ?)",
                    (f"-{int(max_cache_age_sec)} seconds",),
                )
                counts["video_cache"] = max(0, cur.rowcount or 0)
            except Exception as e:
                logger.debug(f"cleanup video_cache notice: {e}")

            conn.commit()
    except Exception as e:
        logger.warning(f"cleanup_old_db_records error: {e}")
    return counts


def get_bot_setting(key: str, default: str = "", db_path: str = None) -> str:
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row and row[0] is not None else default
    except Exception:
        return default


def set_bot_setting(key: str, value: str, db_path: str = None):
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("""
                INSERT INTO bot_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to set bot setting {key}: {e}")


def save_slideshow_session_db(sid: str, data: dict, db_path: str = None):
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO slideshow_sessions
                (sid, url, platform, title, author, total, file_ids_json, photo_urls_json, music, music_info_json, chat_id, created_at, digg_count, comment_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sid,
                data.get("original_url", ""),
                data.get("platform", ""),
                data.get("title", ""),
                data.get("author", ""),
                data.get("total", 0),
                json.dumps(data.get("file_ids", [])),
                json.dumps(data.get("photo_urls", [])),
                data.get("music", ""),
                json.dumps(data.get("music_info") or {}),
                data.get("chat_id", 0),
                data.get("created_at", time.time()),
                data.get("digg_count", 0),
                data.get("comment_count", 0),
            ))
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to save slideshow session {sid}: {e}")


def load_slideshow_session_db(sid: str, db_path: str = None) -> Optional[dict]:
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT url, platform, title, author, total, file_ids_json, photo_urls_json, music, music_info_json, chat_id, created_at, digg_count, comment_count
                    FROM slideshow_sessions WHERE sid = ?
                """, (sid,))
                row = cursor.fetchone()
                if row:
                    u, plat, title, author, total, fids_j, urls_j, mus, mus_inf_j, chat_id, cr_at, digg, comm = row
                    return {
                        "photos": [],
                        "file_ids": json.loads(fids_j) if fids_j else [],
                        "photo_urls": json.loads(urls_j) if urls_j else [],
                        "total": total,
                        "title": title,
                        "author": author,
                        "platform": plat,
                        "original_url": u,
                        "music": mus,
                        "music_info": json.loads(mus_inf_j) if mus_inf_j else {},
                        "digg_count": digg or 0,
                        "comment_count": comm or 0,
                        "chat_id": chat_id,
                        "created_at": cr_at,
                    }
            except Exception:
                cursor.execute(
                    "SELECT url, platform, title, author, total, file_ids_json, photo_urls_json, music, music_info_json, chat_id, created_at FROM slideshow_sessions WHERE sid = ?",
                    (sid,),
                )
                row = cursor.fetchone()
                if row:
                    u, plat, title, author, total, fids_j, urls_j, mus, mus_inf_j, chat_id, cr_at = row
                    return {
                        "photos": [],
                        "file_ids": json.loads(fids_j) if fids_j else [],
                        "photo_urls": json.loads(urls_j) if urls_j else [],
                        "total": total,
                        "title": title,
                        "author": author,
                        "platform": plat,
                        "original_url": u,
                        "music": mus,
                        "music_info": json.loads(mus_inf_j) if mus_inf_j else {},
                        "digg_count": 0,
                        "comment_count": 0,
                        "chat_id": chat_id,
                        "created_at": cr_at,
                    }
    except Exception as e:
        logger.warning(f"Failed to load slideshow session {sid}: {e}")
    return None


def get_canonical_media_key(url: str) -> str:
    """Нормализует любые варианты ссылок одного и того же поста к общему ключу."""
    if not url:
        return ""
    u = url.strip()
    m_ig = re.search(r'(?:instagram\.com|instagr\.am)/(?:reel|p|reels|tv|share/(?:reel|p))/([A-Za-z0-9_-]+)', u)
    if m_ig:
        return f"ig_{m_ig.group(1)}"
    m_yt = re.search(r'(?:v=|\/shorts\/|\/embed\/|youtu\.be\/|\/v\/)([A-Za-z0-9_-]{11})', u)
    if m_yt:
        return f"yt_{m_yt.group(1)}"
    m_tt = re.search(r'/(?:video|photo|v)/(\d+)', u)
    if m_tt:
        return f"tt_{m_tt.group(1)}"
    m_sp = re.search(r'spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]+)', u, re.IGNORECASE)
    if m_sp:
        return f"sp_{m_sp.group(1)}"
    return u.split("?")[0].strip().rstrip("/")


def record_user_download(user_id: int, url: str, file_id: str, media_type: str, title: str, platform: str, db_path: str = None):
    if not user_id or not file_id:
        return
    canon_k = get_canonical_media_key(url)
    clean_u = url.split("?")[0].strip().rstrip("/")
    try:
        with get_db_connection(db_path) as conn:
            conn.execute(
                "DELETE FROM user_downloads WHERE user_id = ? AND (canonical_key = ? OR url = ?)",
                (user_id, canon_k, clean_u),
            )
            conn.execute("""
                INSERT INTO user_downloads (user_id, url, canonical_key, file_id, media_type, title, platform)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, clean_u, canon_k, file_id, media_type, title, platform))
            conn.commit()
    except Exception as e:
        logger.warning(f"Could not record user download: {e}")


def get_user_recent_downloads(user_id: int, limit: int = 15, db_path: str = None) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    items = []
    try:
        with get_db_connection(db_path) as conn:
            rows = conn.execute("""
                SELECT file_id, media_type, title, platform, url
                FROM user_downloads
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (user_id, limit)).fetchall()
            for r in rows:
                items.append({
                    "file_id": r[0],
                    "media_type": r[1] or "video",
                    "title": r[2] or "Медиа",
                    "platform": r[3] or "video",
                    "url": r[4],
                })
    except Exception as e:
        logger.warning(f"Could not get user recent downloads: {e}")
    return items


def save_dl_token(url: str, file_path: str = None, db_path: str = None) -> str:
    if not url:
        return ""
    token = str(uuid.uuid4()).replace("-", "")[:12]
    with get_db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dl_tokens (token, url, file_path) VALUES (?, ?, ?)",
            (token, url, file_path),
        )
        conn.commit()
    return token


def update_dl_token_file(token: str, file_path: str, db_path: str = None):
    if not token or not file_path:
        return
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("UPDATE dl_tokens SET file_path = ? WHERE token = ?", (file_path, token))
            conn.commit()
    except Exception as e:
        logger.debug(f"update_dl_token_file error: {e}")


def get_dl_token_info(token: str, db_path: str = None) -> Tuple[Optional[str], Optional[str]]:
    if not token:
        return None, None
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT url, file_path FROM dl_tokens WHERE token = ?", (token.strip(),))
            row = cursor.fetchone()
            if row:
                return row[0], row[1]
        except Exception:
            cursor.execute("SELECT url FROM dl_tokens WHERE token = ?", (token.strip(),))
            row = cursor.fetchone()
            if row:
                return row[0], None
    return None, None


def get_user_inline_caption(user_id: int, db_path: str = None) -> bool:
    if not user_id:
        return True
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute(
                "SELECT inline_caption FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is not None and row[0] is not None:
                return bool(row[0])
    except Exception:
        pass
    return True


def set_user_inline_caption(user_id: int, enabled: bool, db_path: str = None):
    if not user_id:
        return
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO user_settings (user_id, inline_caption)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                inline_caption = excluded.inline_caption,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, 1 if enabled else 0))
        conn.commit()


def get_user_save_to_pm(user_id: int, db_path: str = None) -> bool:
    if not user_id:
        return False
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute(
                "SELECT save_inline_to_pm FROM user_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is not None and row[0] is not None:
                return bool(row[0])
    except Exception:
        pass
    return False


def set_user_save_to_pm(user_id: int, enabled: bool, db_path: str = None):
    if not user_id:
        return
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("""
                INSERT INTO user_settings (user_id, save_inline_to_pm)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    save_inline_to_pm = excluded.save_inline_to_pm,
                    updated_at = CURRENT_TIMESTAMP
            """, (user_id, 1 if enabled else 0))
            conn.commit()
    except Exception:
        pass


def get_cached_media(url: str, db_path: str = None) -> Optional[Dict[str, Any]]:
    if not url:
        return None
    clean_u = url.split("?")[0].strip().rstrip("/")
    canon_k = get_canonical_media_key(url)
    row = None
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT file_id, title, platform, author, media_type, music, music_info_json FROM video_cache WHERE url = ? OR url = ? LIMIT 1",
                (canon_k, clean_u),
            )
            row = cursor.fetchone()
        except Exception:
            try:
                cursor.execute(
                    "SELECT file_id, title, platform, author, media_type FROM video_cache WHERE url = ? OR url = ? LIMIT 1",
                    (canon_k, clean_u),
                )
                row = cursor.fetchone()
            except Exception:
                cursor.execute(
                    "SELECT file_id, title, platform, author FROM video_cache WHERE url = ? OR url = ? LIMIT 1",
                    (canon_k, clean_u),
                )
                row = cursor.fetchone()
                if row:
                    row = (row[0], row[1], row[2], row[3], "video")
    if row:
        m_inf = None
        if len(row) > 6 and row[6]:
            try:
                m_inf = json.loads(row[6])
            except Exception:
                m_inf = None
        return {
            "file_id": row[0],
            "title": row[1],
            "platform": row[2],
            "author": row[3],
            "media_type": row[4] if len(row) > 4 and row[4] else "video",
            "music": row[5] if len(row) > 5 else None,
            "music_info": m_inf,
        }
    return None


def save_cached_media(
    url: str,
    file_id: str,
    media_type: str,
    title: str,
    platform: str,
    author: str = "",
    music: str = None,
    music_info: dict = None,
    db_path: str = None,
):
    if not url or not file_id:
        return
    clean_u = url.split("?")[0].strip().rstrip("/")
    canon_k = get_canonical_media_key(url)
    m_json = json.dumps(music_info) if isinstance(music_info, dict) else None
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        for u_key in {clean_u, canon_k}:
            if not u_key:
                continue
            try:
                cursor.execute("""
                    INSERT INTO video_cache (url, file_id, media_type, title, platform, author, music, music_info_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        file_id = excluded.file_id,
                        media_type = excluded.media_type,
                        title = excluded.title,
                        platform = excluded.platform,
                        author = excluded.author,
                        music = excluded.music,
                        music_info_json = excluded.music_info_json,
                        created_at = CURRENT_TIMESTAMP
                """, (u_key, file_id, media_type, title, platform, author, music, m_json))
            except Exception:
                cursor.execute("""
                    INSERT INTO video_cache (url, file_id, media_type, title, platform, author)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        file_id = excluded.file_id,
                        media_type = excluded.media_type,
                        title = excluded.title,
                        platform = excluded.platform,
                        author = excluded.author,
                        created_at = CURRENT_TIMESTAMP
                """, (u_key, file_id, media_type, title, platform, author))
        conn.commit()


def delete_cached_media(url: str, db_path: str = None):
    if not url:
        return
    clean_u = url.split("?")[0].strip().rstrip("/")
    canon_k = get_canonical_media_key(url)
    keys = [k for k in {clean_u, canon_k} if k]
    if not keys:
        return
    try:
        with get_db_connection(db_path) as conn:
            conn.executemany("DELETE FROM video_cache WHERE url = ?", [(k,) for k in keys])
            conn.commit()
        logger.info(f"Removed cached media for {url}")
    except Exception as e:
        logger.debug(f"Could not remove cached media: {e}")


def get_any_known_user_id(db_path: str = None) -> Optional[int]:
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute("SELECT user_id FROM users ORDER BY updated_at DESC LIMIT 1").fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return None


def save_user(user_id: int, username: str, first_name: str, photo_url: str = None, gender: str = None, db_path: str = None):
    clean_username = (username or "").lower().replace("@", "").strip()
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, photo_url, gender)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                photo_url = COALESCE(excluded.photo_url, users.photo_url),
                gender = COALESCE(excluded.gender, users.gender),
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, clean_username, first_name, photo_url, gender))
        conn.commit()


def find_user_by_username(username: str, db_path: str = None):
    if not username:
        return None
    s = str(username).strip()
    with get_db_connection(db_path) as conn:
        if s.isdigit():
            row = conn.execute("SELECT user_id, first_name FROM users WHERE user_id = ?", (int(s),)).fetchone()
            if row:
                return row
        clean = s.lower().replace("@", "").strip()
        return conn.execute("SELECT user_id, first_name FROM users WHERE username = ?", (clean,)).fetchone()


def create_invite_record(sender_id, sender_name, recipient_username, place, day, time_str, vibe, duration, note, db_path: str = None):
    clean_recipient = (recipient_username or "").lower().replace("@", "").strip()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO invites (sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sender_id, sender_name, clean_recipient, place, day, time_str, vibe, duration, note))
        invite_id = cursor.lastrowid
        conn.commit()
        return invite_id


def get_invite_record(invite_id: int, db_path: str = None) -> Optional[Dict[str, Any]]:
    with get_db_connection(db_path) as conn:
        row = conn.execute("""
            SELECT id, sender_id, sender_name, recipient_username, place, day, time, vibe, duration, note, status
            FROM invites WHERE id = ?
        """, (invite_id,)).fetchone()
    if row:
        return {
            "id": row[0],
            "sender_id": row[1],
            "sender_name": row[2],
            "recipient_username": row[3],
            "place": row[4],
            "day": row[5],
            "time": row[6],
            "vibe": row[7],
            "duration": row[8],
            "note": row[9],
            "status": row[10],
        }
    return None
