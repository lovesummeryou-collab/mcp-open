from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import auth, db
from .gateway import handle_jsonrpc, rpc_error
from .mcp_client import expand_env
from .registry import ServerRegistry

logger = logging.getLogger("mcp-open.xiaozhi-bridge")

try:
    from websockets.asyncio.client import connect as ws_connect  # type: ignore[attr-defined]
except ImportError:  # websockets < 14
    from websockets.legacy.client import connect as ws_connect  # type: ignore[no-redef]


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for key in list(query):
            if "token" in key.lower() or "key" in key.lower() or "secret" in key.lower():
                query[key] = "***"
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query, doseq=True, safe="*"),
                parts.fragment,
            )
        )
    except Exception:
        return "***"


async def _connect_websocket(url: str) -> Any:
    kwargs: dict[str, Any] = {
        "open_timeout": 10,
        "ping_interval": 20,
        "ping_timeout": 20,
        "close_timeout": 5,
    }
    try:
        return await ws_connect(url, **kwargs)
    except TypeError:
        legacy_kwargs = dict(kwargs)
        legacy_kwargs.pop("close_timeout", None)
        return await ws_connect(url, **legacy_kwargs)


class BridgeConnection:
    def __init__(self, manager: "XiaozhiBridge", row: dict[str, Any]):
        self.manager = manager
        self.id = int(row["id"])
        self.name = str(row["name"])
        self.raw_url = str(row["url"] or "")
        self.url = str(expand_env(self.raw_url) or "").strip()
        self.enabled = bool(row["enabled"])
        self.connected = False
        self.last_error: str | None = None
        self._websocket: Any = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def matches(self, row: dict[str, Any]) -> bool:
        return (
            str(row["name"]) == self.name
            and str(expand_env(row["url"] or "") or "").strip() == self.url
            and bool(row["enabled"]) == self.enabled
        )

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "connected": self.connected,
            "url": redact_url(self.url) if self.url else "",
            "lastError": self.last_error,
        }

    async def start(self) -> None:
        if not self.enabled or not self.url:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=f"xiaozhi-bridge-{self.id}")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.connected = False
        self._websocket = None

    async def notify_tools_changed(self) -> None:
        websocket = self._websocket
        if websocket is None or not self.connected:
            return
        try:
            await websocket.send(
                json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}},
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            logger.debug("桥接 %s 发送工具变更通知失败: %s", self.name, exc)

    async def _run(self) -> None:
        delay = 2.0
        while not self._stop_event.is_set():
            try:
                websocket = await _connect_websocket(self.url)
                self._websocket = websocket
                self.connected = True
                self.last_error = None
                delay = 2.0
                async with websocket:
                    logger.info("小智云端 MCP 已连接: %s -> %s", self.name, redact_url(self.url))
                    await self._notify_initial_tools(websocket)
                    await self._serve(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning(
                    "小智云端桥接 %s 连接失败，%s 秒后重试: %s",
                    self.name,
                    int(delay),
                    exc,
                )
            finally:
                self.connected = False
                self._websocket = None

            if not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 60)

    async def _notify_initial_tools(self, websocket: Any) -> None:
        try:
            await websocket.send(
                json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}},
                    ensure_ascii=False,
                )
            )
        except Exception:
            pass

    async def _serve(self, websocket: Any) -> None:
        principal = auth.Principal(
            token_id=0,
            name=f"xiaozhi-bridge:{self.name}",
            scopes={"mcp:read", "mcp:call", "ws:connect"},
            allowed_services="*",
        )
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                await websocket.send(json.dumps(rpc_error(None, -32700, "JSON 解析失败"), ensure_ascii=False))
                continue
            try:
                response = await handle_jsonrpc(payload, principal, self.manager.registry)
            except Exception as exc:
                logger.exception("处理桥接 %s 的请求失败: %s", self.name, exc)
                response = rpc_error(None, -32603, f"网关异常: {exc}")
            if response is not None:
                await websocket.send(json.dumps(response, ensure_ascii=False))


class XiaozhiBridge:
    """管理多个主动连接小智云端的 MCP WebSocket 桥接。"""

    def __init__(self, registry: ServerRegistry):
        self.registry = registry
        self._connections: dict[int, BridgeConnection] = {}
        self._lock = asyncio.Lock()

    async def apply_settings(self) -> None:
        await self.reload()

    async def reload(self) -> None:
        rows = {
            int(row["id"]): dict(row)
            for row in db.query_all("SELECT * FROM xiaozhi_bridges ORDER BY id")
        }
        async with self._lock:
            for bridge_id, connection in list(self._connections.items()):
                row = rows.get(bridge_id)
                if not row or not row["enabled"] or not connection.matches(row):
                    await connection.stop()
                    self._connections.pop(bridge_id, None)
            for bridge_id, row in rows.items():
                if row["enabled"] and bridge_id not in self._connections:
                    connection = BridgeConnection(self, row)
                    self._connections[bridge_id] = connection
                    await connection.start()

    async def stop(self) -> None:
        async with self._lock:
            for connection in list(self._connections.values()):
                await connection.stop()
            self._connections.clear()

    def status(self) -> list[dict[str, Any]]:
        return [connection.status() for connection in self._connections.values()]

    def status_for(self, bridge_id: int) -> dict[str, Any] | None:
        connection = self._connections.get(int(bridge_id))
        return connection.status() if connection else None

    async def reconnect(self, bridge_id: int) -> bool:
        async with self._lock:
            connection = self._connections.pop(int(bridge_id), None)
            if connection:
                await connection.stop()
            row = db.query_one("SELECT * FROM xiaozhi_bridges WHERE id=?", (int(bridge_id),))
            if not row or not row["enabled"]:
                return False
            new_connection = BridgeConnection(self, dict(row))
            self._connections[int(bridge_id)] = new_connection
            await new_connection.start()
            return True

    async def notify_tools_changed(self) -> None:
        for connection in list(self._connections.values()):
            await connection.notify_tools_changed()
