from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import DB_PATH, SQLITE_JOURNAL
from .security import hash_password

_LOCK = threading.RLock()
_LOGGER = logging.getLogger("mcp-open.db")
_WAL_FALLBACK_LOGGED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _configure_journal(conn: sqlite3.Connection) -> None:
    global _WAL_FALLBACK_LOGGED
    conn.execute("PRAGMA busy_timeout=15000")

    def try_mode(mode: str, warn: bool = False) -> bool:
        try:
            conn.execute(f"PRAGMA journal_mode={mode}")
            return True
        except sqlite3.OperationalError as exc:
            if warn and not _WAL_FALLBACK_LOGGED:
                _LOGGER.warning("SQLite %s 模式不可用，改用默认模式: %s", mode, exc)
                _WAL_FALLBACK_LOGGED = True
            return False

    if SQLITE_JOURNAL == "wal":
        try_mode("WAL", warn=True)
        return
    if SQLITE_JOURNAL == "delete":
        try_mode("DELETE")
        return
    if not try_mode("WAL"):
        try_mode("DELETE", warn=True)


@contextmanager
def connect():
    with _LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _configure_journal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as conn:
        cursor = conn.execute(sql, tuple(params))
        return cursor.lastrowid or cursor.rowcount


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                transport TEXT NOT NULL DEFAULT 'stdio',
                command TEXT,
                args TEXT NOT NULL DEFAULT '[]',
                cwd TEXT,
                env TEXT NOT NULL DEFAULT '{}',
                url TEXT,
                headers TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                timeout REAL NOT NULL DEFAULT 30,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT 'mcp:read,mcp:call',
                allowed_services TEXT NOT NULL DEFAULT '*',
                is_active INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                created_by TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS xiaozhi_bridges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        from .config import ADMIN_PASSWORD, ADMIN_USERNAME, DEFAULT_TIMEOUT

        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if count == 0:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?)",
                (ADMIN_USERNAME, hash_password(ADMIN_PASSWORD), "admin", 1, utc_now()),
            )

        defaults = {
            "server_name": "mcp-open",
            "auth_authorizer": "",
            "request_timeout": str(DEFAULT_TIMEOUT),
            "tool_name_separator": "__",
            "xiaozhi_welcome": "已连接 mcp-open 工具网关",
            "xiaozhi_bridge_url": "",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?,?,?)",
                (key, value, utc_now()),
            )

        legacy = conn.execute(
            "SELECT value FROM settings WHERE key='xiaozhi_bridge_url'"
        ).fetchone()
        if legacy and legacy["value"].strip():
            count = conn.execute("SELECT COUNT(*) AS c FROM xiaozhi_bridges").fetchone()["c"]
            if count == 0:
                conn.execute(
                    "INSERT INTO xiaozhi_bridges(name, url, enabled, created_at, updated_at) VALUES (?,?,?,?,?)",
                    ("默认小智云端", legacy["value"].strip(), 1, utc_now(), utc_now()),
                )
            conn.execute("UPDATE settings SET value='' WHERE key='xiaozhi_bridge_url'")


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def get_settings() -> dict[str, str]:
    return {row["key"]: row["value"] for row in query_all("SELECT key, value FROM settings")}


def set_settings(values: dict[str, Any], actor: str = "system") -> dict[str, str]:
    for key, value in values.items():
        execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value, utc_now()),
        )
    audit(actor, "settings.update", ",".join(values.keys()), json.dumps(values, ensure_ascii=False))
    return get_settings()


def audit(actor: str, action: str, target: str | None = None, detail: str | None = None) -> None:
    execute(
        "INSERT INTO audit_logs(created_at, actor, action, target, detail) VALUES (?,?,?,?,?)",
        (utc_now(), actor, action, target, detail),
    )
