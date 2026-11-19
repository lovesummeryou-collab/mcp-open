from __future__ import annotations

import importlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request, status

from . import db
from .config import AUTH_AUTHORIZER, TOKEN_HEADER, TOKEN_QUERY_NAMES
from .security import hash_token, verify_session

logger = logging.getLogger("mcp-open.auth")

ALL_SCOPES = {
    "admin",
    "mcp:read",
    "mcp:call",
    "ws:connect",
    "servers:read",
    "tokens:read",
}


@dataclass
class Principal:
    token_id: int
    name: str
    scopes: set[str] = field(default_factory=set)
    allowed_services: Any = "*"
    role: str = "user"
    username: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.token_id,
            "name": self.name,
            "scopes": sorted(self.scopes),
            "allowed_services": self.allowed_services,
            "role": self.role,
            "username": self.username,
        }


def _extract_token(request: Request | None, websocket: Any | None = None) -> str | None:
    if websocket is not None:
        for name in TOKEN_QUERY_NAMES:
            value = websocket.query_params.get(name)
            if value:
                return value
        auth = websocket.headers.get(TOKEN_HEADER, "")
    else:
        if request is None:
            return None
        for name in TOKEN_QUERY_NAMES:
            value = request.query_params.get(name)
            if value:
                return value
        auth = request.headers.get(TOKEN_HEADER, "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip() or None


def _parse_allowed_services(raw: Any) -> Any:
    if raw is None:
        return "*"
    if isinstance(raw, str):
        value = raw.strip()
        if not value or value == "*":
            return "*"
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = [item.strip() for item in value.split(",") if item.strip()]
        if parsed == "*":
            return "*"
        return [str(item) for item in parsed] if isinstance(parsed, list) else [str(parsed)]
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw]
    return [str(raw)]


def principal_from_token(token: str | None) -> Principal:
    if not token:
        raise HTTPException(status_code=401, detail="缺少 API Token")
    row = db.query_one("SELECT * FROM tokens WHERE token_hash=?", (hash_token(token),))
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="API Token 无效或已禁用")
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=401, detail="API Token 已过期")
    db.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (db.utc_now(), row["id"]))
    scopes = {item.strip() for item in (row["scopes"] or "").split(",") if item.strip()}
    return Principal(
        token_id=row["id"],
        name=row["name"],
        scopes=scopes,
        allowed_services=_parse_allowed_services(row["allowed_services"]),
        username=row["created_by"],
    )


def require_api_token(request: Request | None = None, websocket: Any | None = None) -> Principal:
    return principal_from_token(_extract_token(request, websocket))


def require_scopes(principal: Principal, *scopes: str) -> None:
    missing = [scope for scope in scopes if scope not in principal.scopes and "admin" not in principal.scopes]
    if missing:
        raise HTTPException(status_code=403, detail=f"Token 缺少权限: {', '.join(missing)}")


def require_admin(request: Request) -> str:
    session = verify_session(request.cookies.get("mcp_session"))
    if session:
        row = db.query_one("SELECT * FROM users WHERE username=?", (session["sub"],))
        if row and row["is_active"]:
            return str(row["username"])
    token = _extract_token(request)
    if token:
        principal = require_api_token(request)
        if "admin" in principal.scopes:
            return principal.username or principal.name
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录管理后台")


def _custom_authorizer() -> Any:
    configured = db.get_setting("auth_authorizer", "") or AUTH_AUTHORIZER
    if not configured or ":" not in configured:
        return None
    module_path, attr_path = configured.split(":", 1)
    try:
        module = importlib.import_module(module_path)
        target: Any = module
        for part in attr_path.split("."):
            target = getattr(target, part)
        if inspect.isclass(target):
            target = target()
        return target
    except Exception as exc:  # pragma: no cover - 配置错误时保留服务可用
        logger.warning("加载自定义 authorizer 失败: %s", exc)
        return None


async def authorize(
    principal: Principal,
    action: str,
    *,
    request: Request | None = None,
    websocket: Any | None = None,
    service_id: int | None = None,
    service_name: str | None = None,
    tool_name: str | None = None,
    raise_on_deny: bool = True,
) -> bool:
    allowed = True
    if service_id is not None and principal.allowed_services != "*":
        allowed = str(service_id) in set(principal.allowed_services)

    authorizer = _custom_authorizer()
    if authorizer is not None:
        context = {
            "principal": principal.as_dict(),
            "action": action,
            "service_id": service_id,
            "service_name": service_name,
            "tool_name": tool_name,
            "request_path": request.url.path if request else (websocket.url.path if websocket else None),
            "method": request.method if request else None,
            "client_ip": request.client.host if request and request.client else None,
        }
        callback = getattr(authorizer, "authorize", authorizer)
        try:
            result = callback(context)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                allowed = allowed and bool(result)
        except Exception as exc:
            logger.warning("自定义 authorizer 执行失败: %s", exc)
            allowed = False

    if not allowed and raise_on_deny:
        raise HTTPException(status_code=403, detail="没有访问该资源的权限")
    return allowed
