from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = APP_DIR / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_secret() -> str:
    configured = os.getenv("MCPOPEN_SECRET_KEY")
    if configured:
        return configured
    secret_file = DATA_DIR / "secret.key"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    secret_file.write_text(value, encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return value


DB_PATH = Path(os.getenv("MCPOPEN_DB", str(DATA_DIR / "mcp-open.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SECRET_KEY = _load_secret()
ADMIN_USERNAME = os.getenv("MCPOPEN_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("MCPOPEN_ADMIN_PASSWORD", "admin123")
SERVER_NAME = os.getenv("MCPOPEN_SERVER_NAME", "mcp-open")
DEFAULT_TIMEOUT = float(os.getenv("MCPOPEN_TIMEOUT", "30"))
AUTH_AUTHORIZER = os.getenv("MCPOPEN_AUTH_AUTHORIZER", "")
CORS_ORIGINS = [item.strip() for item in os.getenv("MCPOPEN_CORS_ORIGINS", "*").split(",") if item.strip()]
SESSION_MAX_AGE = int(os.getenv("MCPOPEN_SESSION_MAX_AGE", str(7 * 24 * 3600)))
DEBUG = _env_bool("MCPOPEN_DEBUG", False)
TOKEN_HEADER = "authorization"
TOKEN_QUERY_NAMES = ("token", "api_key", "access_token")
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
