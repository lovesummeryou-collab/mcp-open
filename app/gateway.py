from __future__ import annotations

import asyncio
from typing import Any

from . import auth
from .config import PROTOCOL_VERSION, SERVER_NAME, SUPPORTED_PROTOCOL_VERSIONS
from .mcp_client import McpError
from .registry import ServerRegistry


def rpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def rpc_error(message_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


async def _handle_one(
    payload: Any,
    principal: auth.Principal,
    registry: ServerRegistry,
    request: Any = None,
    websocket: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return rpc_error(None, -32600, "无效的请求")

    message_id = payload.get("id")
    notification = "id" not in payload
    method = payload["method"]
    params = payload.get("params") or {}

    if method.startswith("notifications/") or method in {"initialized"}:
        return None

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        return rpc_result(
            message_id,
            {
                "protocolVersion": version,
                "capabilities": {
                    "tools": {"listChanged": True},
                    "logging": {},
                    "experimental": {},
                },
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                "instructions": "已通过 mcp-open 聚合本地与远端 MCP 服务；工具名使用 服务名__工具名 格式。",
            },
        )

    if method == "ping":
        return rpc_result(message_id, {})

    if method in {"tools/list", "tools/call"}:
        required_scope = "mcp:read" if method == "tools/list" else "mcp:call"
        if required_scope not in principal.scopes and "admin" not in principal.scopes:
            if notification:
                return None
            return rpc_error(message_id, -32001, f"Token 缺少权限: {required_scope}")

    if method == "tools/list":
        allowed = await auth.authorize(
            principal,
            "tools:list",
            request=request,
            websocket=websocket,
            raise_on_deny=False,
        )
        if not allowed:
            return None if notification else rpc_error(message_id, -32001, "没有列出工具权限")
        try:
            tools = await registry.list_tools(principal.allowed_services)
        except Exception as exc:
            return None if notification else rpc_error(message_id, -32603, str(exc))
        return None if notification else rpc_result(message_id, {"tools": tools})

    if method == "tools/call":
        if not isinstance(params, dict):
            return None if notification else rpc_error(message_id, -32602, "params 必须是对象")
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return None if notification else rpc_error(message_id, -32602, "name 必须是字符串，arguments 必须是对象")
        try:
            info = await registry.resolve_tool(tool_name, principal.allowed_services)
        except McpError as exc:
            return None if notification else rpc_error(message_id, exc.code, str(exc), exc.data)
        allowed = await auth.authorize(
            principal,
            "tools:call",
            request=request,
            websocket=websocket,
            service_id=int(info["service_id"]),
            service_name=info["service_name"],
            tool_name=info["tool_name"],
            raise_on_deny=False,
        )
        if not allowed:
            return None if notification else rpc_error(message_id, -32001, "没有调用该工具的权限")
        try:
            result, _ = await registry.call_tool(tool_name, arguments, principal.allowed_services)
        except McpError as exc:
            return None if notification else rpc_error(message_id, exc.code, str(exc), exc.data)
        except Exception as exc:
            return None if notification else rpc_error(message_id, -32603, f"工具调用失败: {exc}")
        return None if notification else rpc_result(message_id, result)

    if method in {"resources/list", "resources/templates/list", "prompts/list"}:
        return None if notification else rpc_result(message_id, {"resources": [], "prompts": []})

    if notification:
        return None
    return rpc_error(message_id, -32601, f"不支持的方法: {method}")


async def handle_jsonrpc(
    payload: Any,
    principal: auth.Principal,
    registry: ServerRegistry,
    request: Any = None,
    websocket: Any = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        if not payload:
            return rpc_error(None, -32600, "空的批量请求")
        results = await asyncio.gather(
            *(_handle_one(item, principal, registry, request, websocket) for item in payload)
        )
        responses = [item for item in results if item is not None]
        return responses or None
    return await _handle_one(payload, principal, registry, request, websocket)
