from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from . import db
from .auth import ALL_SCOPES, require_admin
from .mcp_client import McpError
from .registry import ServerRegistry, as_dict
from .schemas import (
    BridgeCreate,
    BridgeUpdate,
    LoginRequest,
    ServerCreate,
    ServerUpdate,
    SettingsUpdate,
    TokenCreate,
    TokenUpdate,
    UserCreate,
    UserUpdate,
)
from .security import (
    create_session,
    generate_token,
    hash_password,
    hash_token,
    mask_token,
    verify_password,
    verify_session,
)
from .xiaozhi_bridge import redact_url

router = APIRouter(prefix="/api")


def get_registry(request: Request) -> ServerRegistry:
    return request.app.state.registry


def _server_out(row: Any) -> dict[str, Any]:
    return as_dict(row)


def _token_out(row: Any) -> dict[str, Any]:
    value = dict(row)
    try:
        allowed = json.loads(value.get("allowed_services") or "*")
    except ValueError:
        allowed = value.get("allowed_services") or "*"

    allowed_service_names: list[str] = []
    if allowed == "*" or (isinstance(allowed, list) and "*" in allowed):
        allowed_service_names = ["全部服务"]
    elif isinstance(allowed, list) and allowed:
        placeholders = ",".join("?" for _ in allowed)
        rows = db.query_all(f"SELECT id, name FROM servers WHERE id IN ({placeholders})", allowed)
        name_by_id = {str(row["id"]): row["name"] for row in rows}
        allowed_service_names = [name_by_id.get(str(item), f"#{item}") for item in allowed]

    return {
        "id": value["id"],
        "name": value["name"],
        "prefix": mask_token(value["prefix"]),
        "prefixFull": value["prefix"],
        "scopes": [item for item in (value["scopes"] or "").split(",") if item],
        "allowed_services": allowed,
        "allowed_service_names": allowed_service_names,
        "is_active": bool(value["is_active"]),
        "expires_at": value["expires_at"],
        "created_at": value["created_at"],
        "last_used_at": value["last_used_at"],
        "created_by": value["created_by"],
    }


def _user_out(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
    }


def _bridge_out(row: Any, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    value = dict(row)
    runtime = runtime or {}
    return {
        "id": value["id"],
        "name": value["name"],
        "url": value["url"],
        "displayUrl": redact_url(str(value["url"] or "")),
        "enabled": bool(value["enabled"]),
        "connected": bool(runtime.get("connected", False)),
        "lastError": runtime.get("lastError"),
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
    }


def _allowed_services_text(value: str | list[int] | list[str] | None) -> str:
    if value in (None, "", "*", ["*"]):
        return "*"
    return json.dumps([str(item) for item in value], ensure_ascii=False)


def _validate_scopes(scopes: list[str] | None) -> list[str]:
    scopes = scopes or []
    unknown = [scope for scope in scopes if scope not in ALL_SCOPES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知权限: {', '.join(unknown)}")
    return scopes


@router.post("/auth/login")
def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM users WHERE username=?", (payload.username,))
    if not row or not row["is_active"] or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(
        "mcp_session",
        create_session(row["username"]),
        httponly=True,
        samesite="lax",
        max_age=int(db.get_setting("session_max_age", "604800") or 604800),
    )
    db.audit(row["username"], "auth.login", row["username"])
    return {"username": row["username"], "role": row["role"]}


@router.post("/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("mcp_session")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request) -> dict[str, Any]:
    session = verify_session(request.cookies.get("mcp_session"))
    if not session:
        raise HTTPException(status_code=401, detail="未登录")
    row = db.query_one("SELECT * FROM users WHERE username=?", (session["sub"],))
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="用户不可用")
    return _user_out(row)


@router.get("/overview")
def overview(request: Request, admin: str = Depends(require_admin)) -> dict[str, Any]:
    registry = get_registry(request)
    tools = registry._cache
    return {
        "servers": db.query_one("SELECT COUNT(*) AS c FROM servers")["c"],
        "enabledServers": db.query_one("SELECT COUNT(*) AS c FROM servers WHERE enabled=1")["c"],
        "tokens": db.query_one("SELECT COUNT(*) AS c FROM tokens WHERE is_active=1")["c"],
        "users": db.query_one("SELECT COUNT(*) AS c FROM users WHERE is_active=1")["c"],
        "tools": len(tools),
        "registry": registry.stats(),
        "xiaozhiBridge": request.app.state.bridge.status(),
        "recentAudits": [dict(row) for row in db.query_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 8")],
    }


@router.get("/servers")
async def list_servers(request: Request, admin: str = Depends(require_admin)) -> list[dict[str, Any]]:
    registry = get_registry(request)
    await registry.refresh_tools()
    grouped = registry.tools_by_server()
    result = []
    for row in db.query_all("SELECT * FROM servers ORDER BY id"):
        item = _server_out(row)
        tools = grouped.get(str(row["id"]), [])
        item["toolCount"] = len(tools)
        item["tools"] = tools
        result.append(item)
    return result


@router.post("/servers", status_code=status.HTTP_201_CREATED)
def create_server(payload: ServerCreate, admin: str = Depends(require_admin)) -> dict[str, Any]:
    now = db.utc_now()
    try:
        server_id = db.execute(
            "INSERT INTO servers(name, transport, command, args, cwd, env, url, headers, enabled, timeout, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload.name,
                payload.transport,
                payload.command,
                json.dumps(payload.args, ensure_ascii=False),
                payload.cwd,
                json.dumps(payload.env, ensure_ascii=False),
                payload.url,
                json.dumps(payload.headers, ensure_ascii=False),
                int(payload.enabled),
                payload.timeout,
                now,
                now,
            ),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=400, detail="服务名称已存在") from exc
        raise
    db.audit(admin, "server.create", payload.name)
    return _server_out(db.query_one("SELECT * FROM servers WHERE id=?", (server_id,)))


@router.post("/servers/refresh-tools")
async def refresh_tools(request: Request, admin: str = Depends(require_admin)) -> dict[str, Any]:
    registry = get_registry(request)
    await registry.refresh_tools(force=True)
    db.audit(admin, "server.refresh_tools")
    return {"ok": True, "tools": len(registry._cache)}


@router.get("/servers/{server_id}")
def get_server(server_id: int, admin: str = Depends(require_admin)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM servers WHERE id=?", (server_id,))
    if not row:
        raise HTTPException(status_code=404, detail="服务不存在")
    return _server_out(row)


@router.put("/servers/{server_id}")
async def update_server(
    server_id: int,
    payload: ServerUpdate,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    if not db.query_one("SELECT id FROM servers WHERE id=?", (server_id,)):
        raise HTTPException(status_code=404, detail="服务不存在")
    db.execute(
        "UPDATE servers SET name=?, transport=?, command=?, args=?, cwd=?, env=?, url=?, headers=?, enabled=?, timeout=?, updated_at=? WHERE id=?",
        (
            payload.name,
            payload.transport,
            payload.command,
            json.dumps(payload.args, ensure_ascii=False),
            payload.cwd,
            json.dumps(payload.env, ensure_ascii=False),
            payload.url,
            json.dumps(payload.headers, ensure_ascii=False),
            int(payload.enabled),
            payload.timeout,
            db.utc_now(),
            server_id,
        ),
    )
    await get_registry(request).invalidate(server_id)
    db.audit(admin, "server.update", payload.name)
    return _server_out(db.query_one("SELECT * FROM servers WHERE id=?", (server_id,)))


@router.delete("/servers/{server_id}")
async def delete_server(server_id: int, request: Request, admin: str = Depends(require_admin)) -> dict[str, bool]:
    row = db.query_one("SELECT name FROM servers WHERE id=?", (server_id,))
    if not row:
        raise HTTPException(status_code=404, detail="服务不存在")
    db.execute("DELETE FROM servers WHERE id=?", (server_id,))
    await get_registry(request).invalidate(server_id)
    db.audit(admin, "server.delete", row["name"])
    return {"ok": True}


@router.post("/servers/{server_id}/test")
async def test_server(server_id: int, request: Request, admin: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        return await get_registry(request).test_server(server_id)
    except McpError as exc:
        return {"id": server_id, "ok": False, "error": str(exc), "code": exc.code}
    except Exception as exc:
        return {"id": server_id, "ok": False, "error": str(exc)}


@router.get("/servers/{server_id}/tools")
async def server_tools(
    server_id: int,
    request: Request,
    refresh: bool = Query(default=False),
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        tools = await get_registry(request).list_tools_for_server(server_id, refresh=refresh)
    except McpError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tools": tools}


@router.get("/bridges")
def list_bridges(request: Request, admin: str = Depends(require_admin)) -> list[dict[str, Any]]:
    bridge = request.app.state.bridge
    return [
        _bridge_out(row, bridge.status_for(int(row["id"])))
        for row in db.query_all("SELECT * FROM xiaozhi_bridges ORDER BY id")
    ]


@router.post("/bridges", status_code=status.HTTP_201_CREATED)
async def create_bridge(
    payload: BridgeCreate,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    now = db.utc_now()
    try:
        bridge_id = db.execute(
            "INSERT INTO xiaozhi_bridges(name, url, enabled, created_at, updated_at) VALUES (?,?,?,?,?)",
            (payload.name, payload.url, int(payload.enabled), now, now),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=400, detail="桥接名称已存在") from exc
        raise
    await request.app.state.bridge.reload()
    db.audit(admin, "bridge.create", payload.name)
    row = db.query_one("SELECT * FROM xiaozhi_bridges WHERE id=?", (bridge_id,))
    return _bridge_out(row, request.app.state.bridge.status_for(int(bridge_id)))


@router.put("/bridges/{bridge_id}")
async def update_bridge(
    bridge_id: int,
    payload: BridgeUpdate,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM xiaozhi_bridges WHERE id=?", (bridge_id,))
    if not row:
        raise HTTPException(status_code=404, detail="桥接不存在")
    name = payload.name if payload.name is not None else row["name"]
    url = payload.url if payload.url is not None else row["url"]
    enabled = int(payload.enabled) if payload.enabled is not None else row["enabled"]
    try:
        db.execute(
            "UPDATE xiaozhi_bridges SET name=?, url=?, enabled=?, updated_at=? WHERE id=?",
            (name, url, enabled, db.utc_now(), bridge_id),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=400, detail="桥接名称已存在") from exc
        raise
    await request.app.state.bridge.reload()
    db.audit(admin, "bridge.update", name)
    updated = db.query_one("SELECT * FROM xiaozhi_bridges WHERE id=?", (bridge_id,))
    return _bridge_out(updated, request.app.state.bridge.status_for(bridge_id))


@router.delete("/bridges/{bridge_id}")
async def delete_bridge(
    bridge_id: int,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, bool]:
    row = db.query_one("SELECT name FROM xiaozhi_bridges WHERE id=?", (bridge_id,))
    if not row:
        raise HTTPException(status_code=404, detail="桥接不存在")
    db.execute("DELETE FROM xiaozhi_bridges WHERE id=?", (bridge_id,))
    await request.app.state.bridge.reload()
    db.audit(admin, "bridge.delete", row["name"])
    return {"ok": True}


@router.post("/bridges/{bridge_id}/reconnect")
async def reconnect_bridge(
    bridge_id: int,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    ok = await request.app.state.bridge.reconnect(bridge_id)
    if not ok:
        raise HTTPException(status_code=400, detail="桥接不存在或已停用")
    db.audit(admin, "bridge.reconnect", str(bridge_id))
    row = db.query_one("SELECT * FROM xiaozhi_bridges WHERE id=?", (bridge_id,))
    return _bridge_out(row, request.app.state.bridge.status_for(bridge_id))


@router.get("/tokens")
def list_tokens(admin: str = Depends(require_admin)) -> list[dict[str, Any]]:
    return [_token_out(row) for row in db.query_all("SELECT * FROM tokens ORDER BY id DESC")]


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
def create_token(payload: TokenCreate, admin: str = Depends(require_admin)) -> dict[str, Any]:
    scopes = _validate_scopes(payload.scopes)
    raw_token, prefix = generate_token(payload.name)
    expires_at = None
    if payload.expires_in_days:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)).isoformat(timespec="seconds")
    token_id = db.execute(
        "INSERT INTO tokens(name, token_hash, prefix, scopes, allowed_services, is_active, expires_at, created_at, created_by) "
        "VALUES (?,?,?,?,?,1,?,?,?)",
        (
            payload.name,
            hash_token(raw_token),
            prefix,
            ",".join(scopes),
            _allowed_services_text(payload.allowed_services),
            expires_at,
            db.utc_now(),
            admin,
        ),
    )
    db.audit(admin, "token.create", payload.name)
    result = _token_out(db.query_one("SELECT * FROM tokens WHERE id=?", (token_id,)))
    result["token"] = raw_token
    return result


@router.put("/tokens/{token_id}")
def update_token(
    token_id: int,
    payload: TokenUpdate,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM tokens WHERE id=?", (token_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Token 不存在")
    scopes = _validate_scopes(payload.scopes) if payload.scopes is not None else [
        item for item in (row["scopes"] or "").split(",") if item
    ]
    allowed = (
        _allowed_services_text(payload.allowed_services)
        if payload.allowed_services is not None
        else row["allowed_services"]
    )
    db.execute(
        "UPDATE tokens SET name=?, scopes=?, allowed_services=?, is_active=? WHERE id=?",
        (
            payload.name or row["name"],
            ",".join(scopes),
            allowed,
            int(payload.is_active) if payload.is_active is not None else row["is_active"],
            token_id,
        ),
    )
    db.audit(admin, "token.update", payload.name or row["name"])
    return _token_out(db.query_one("SELECT * FROM tokens WHERE id=?", (token_id,)))


@router.post("/tokens/{token_id}/regenerate")
def regenerate_token(
    token_id: int,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM tokens WHERE id=?", (token_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Token 不存在")
    raw_token, prefix = generate_token(row["name"])
    db.execute(
        "UPDATE tokens SET token_hash=?, prefix=?, last_used_at=NULL WHERE id=?",
        (hash_token(raw_token), prefix, token_id),
    )
    db.audit(admin, "token.regenerate", row["name"])
    result = _token_out(db.query_one("SELECT * FROM tokens WHERE id=?", (token_id,)))
    result["token"] = raw_token
    return result


@router.delete("/tokens/{token_id}")
def delete_token(token_id: int, admin: str = Depends(require_admin)) -> dict[str, bool]:
    row = db.query_one("SELECT name FROM tokens WHERE id=?", (token_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Token 不存在")
    db.execute("DELETE FROM tokens WHERE id=?", (token_id,))
    db.audit(admin, "token.delete", row["name"])
    return {"ok": True}


@router.get("/users")
def list_users(admin: str = Depends(require_admin)) -> list[dict[str, Any]]:
    return [_user_out(row) for row in db.query_all("SELECT * FROM users ORDER BY id")]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, admin: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        user_id = db.execute(
            "INSERT INTO users(username, password_hash, role, is_active, created_at) VALUES (?,?,?,?,?)",
            (payload.username, hash_password(payload.password), payload.role, 1, db.utc_now()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=400, detail="用户名已存在") from exc
        raise
    db.audit(admin, "user.create", payload.username)
    return _user_out(db.query_one("SELECT * FROM users WHERE id=?", (user_id,)))


@router.put("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, admin: str = Depends(require_admin)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    password_hash = hash_password(payload.password) if payload.password else row["password_hash"]
    db.execute(
        "UPDATE users SET password_hash=?, role=?, is_active=? WHERE id=?",
        (
            password_hash,
            payload.role or row["role"],
            int(payload.is_active) if payload.is_active is not None else row["is_active"],
            user_id,
        ),
    )
    db.audit(admin, "user.update", row["username"])
    return _user_out(db.query_one("SELECT * FROM users WHERE id=?", (user_id,)))


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: str = Depends(require_admin)) -> dict[str, bool]:
    row = db.query_one("SELECT username FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["username"] == admin:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.audit(admin, "user.delete", row["username"])
    return {"ok": True}


@router.get("/settings")
def get_settings(request: Request, admin: str = Depends(require_admin)) -> dict[str, Any]:
    return {
        "settings": db.get_settings(),
        "xiaozhiBridge": request.app.state.bridge.status(),
        "availableScopes": sorted(ALL_SCOPES),
        "version": "0.1.0",
    }


@router.put("/settings")
async def update_settings(
    payload: SettingsUpdate,
    request: Request,
    admin: str = Depends(require_admin),
) -> dict[str, Any]:
    allowed = {
        "server_name",
        "auth_authorizer",
        "request_timeout",
        "tool_name_separator",
        "xiaozhi_welcome",
        "xiaozhi_bridge_url",
        "tool_cache_ttl",
        "session_max_age",
    }
    unknown = set(payload.values) - allowed
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的设置项: {', '.join(sorted(unknown))}")
    settings = db.set_settings(payload.values, admin)
    await request.app.state.bridge.reload()
    return {"settings": settings, "xiaozhiBridge": request.app.state.bridge.status()}


@router.get("/audit")
def list_audit(limit: int = Query(default=100, ge=1, le=1000), admin: str = Depends(require_admin)) -> list[dict[str, Any]]:
    return [dict(row) for row in db.query_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))]
