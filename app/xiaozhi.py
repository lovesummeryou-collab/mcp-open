from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from . import auth, db
from .gateway import handle_jsonrpc
from .mcp_client import McpError
from .registry import ServerRegistry

logger = logging.getLogger("mcp-open.xiaozhi")


async def _send_error(websocket: WebSocket, message: str, code: int = -32000, request_id: Any = None) -> None:
    await websocket.send_json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _tool_payload(result: Any) -> tuple[Any, bool]:
    if isinstance(result, dict):
        content = result.get("content")
        is_error = bool(result.get("isError", False))
        if content is None:
            content = [
                {
                    "type": "text",
                    "text": json.dumps(result.get("structuredContent", result), ensure_ascii=False),
                }
            ]
        return content, is_error
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], False


async def websocket_gateway(websocket: WebSocket, registry: ServerRegistry) -> None:
    await websocket.accept()
    principal: auth.Principal | None = None

    try:
        principal = auth.require_api_token(websocket=websocket)
        auth.require_scopes(principal, "ws:connect")
    except HTTPException:
        pass

    if principal is None:
        try:
            first = json.loads(await websocket.receive_text())
        except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
            await _send_error(websocket, "需要先发送认证信息")
            await websocket.close(code=1008)
            return
        token = first.get("token") or first.get("api_key") if isinstance(first, dict) else None
        try:
            principal = auth.principal_from_token(token)
            auth.require_scopes(principal, "ws:connect")
        except HTTPException as exc:
            await _send_error(websocket, str(exc.detail), -32001, first.get("id") if isinstance(first, dict) else None)
            await websocket.close(code=1008)
            return

    await websocket.send_json(
        {
            "type": "hello",
            "protocol": "mcp-open-xiaozhi-v1",
            "server": db.get_setting("server_name", "mcp-open"),
            "message": db.get_setting("xiaozhi_welcome", "已连接 mcp-open 工具网关"),
            "capabilities": {"tools": True, "jsonrpc": True},
        }
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except ValueError:
                await _send_error(websocket, "JSON 解析失败", -32700)
                continue

            if isinstance(payload, list):
                response = await handle_jsonrpc(payload, principal, registry, websocket=websocket)
                if response is not None:
                    await websocket.send_json(response)
                continue

            if not isinstance(payload, dict):
                await _send_error(websocket, "消息必须是对象", -32600)
                continue

            message_type = str(payload.get("type") or "")
            request_id = payload.get("id")

            if message_type in {"auth", "authenticate"}:
                await websocket.send_json({"type": "auth_ok", "principal": principal.as_dict()})
                continue

            if message_type == "ping":
                await websocket.send_json({"type": "pong", "id": request_id})
                continue

            if message_type in {"tools/list", "list_tools"}:
                try:
                    auth.require_scopes(principal, "mcp:read")
                    allowed = await auth.authorize(
                        principal,
                        "tools:list",
                        websocket=websocket,
                        raise_on_deny=False,
                    )
                    if not allowed:
                        raise HTTPException(status_code=403, detail="没有列出工具权限")
                    tools = await registry.list_tools(principal.allowed_services)
                except (HTTPException, Exception) as exc:
                    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                    await _send_error(websocket, str(detail), -32001, request_id)
                    continue
                await websocket.send_json({"type": "tools/list", "id": request_id, "tools": tools})
                continue

            if message_type in {"tool_call", "call_tool", "tools/call"}:
                name = payload.get("name") or (payload.get("params") or {}).get("name")
                arguments = payload.get("arguments") or (payload.get("params") or {}).get("arguments") or {}
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    await _send_error(websocket, "name/arguments 参数无效", -32602, request_id)
                    continue
                try:
                    auth.require_scopes(principal, "mcp:call")
                    info = await registry.resolve_tool(name, principal.allowed_services)
                    allowed = await auth.authorize(
                        principal,
                        "tools:call",
                        websocket=websocket,
                        service_id=int(info["service_id"]),
                        service_name=info["service_name"],
                        tool_name=info["tool_name"],
                        raise_on_deny=False,
                    )
                    if not allowed:
                        raise HTTPException(status_code=403, detail="没有调用该工具的权限")
                    result, _ = await registry.call_tool(name, arguments, principal.allowed_services)
                    content, is_error = _tool_payload(result)
                except McpError as exc:
                    content = [{"type": "text", "text": str(exc)}]
                    is_error = True
                except HTTPException as exc:
                    content = [{"type": "text", "text": str(exc.detail)}]
                    is_error = True
                except Exception as exc:
                    content = [{"type": "text", "text": f"工具调用失败: {exc}"}]
                    is_error = True
                await websocket.send_json(
                    {
                        "type": "tool_result",
                        "id": request_id,
                        "name": name,
                        "content": content,
                        "isError": is_error,
                    }
                )
                continue

            if "jsonrpc" in payload:
                response = await handle_jsonrpc(payload, principal, registry, websocket=websocket)
                if response is not None:
                    await websocket.send_json(response)
                continue

            await _send_error(websocket, f"未知消息类型: {message_type}", -32601, request_id)
    except WebSocketDisconnect:
        logger.info("WebSocket 客户端断开")
    except Exception as exc:  # pragma: no cover - 防御性处理
        logger.exception("WebSocket 网关异常: %s", exc)
        with_context = _send_error(websocket, f"网关异常: {exc}")
        try:
            await with_context
        except Exception:
            pass
