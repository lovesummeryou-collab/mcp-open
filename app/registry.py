from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from . import db
from .mcp_client import BaseMcpClient, McpError, create_client

logger = logging.getLogger("mcp-open.registry")


def slugify(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return result or "server"


class ServerRegistry:
    def __init__(self) -> None:
        self._clients: dict[int, BaseMcpClient] = {}
        self._cache: list[dict[str, Any]] = []
        self._mapping: dict[str, dict[str, Any]] = {}
        self._cache_time = 0.0
        self._cache_lock = asyncio.Lock()
        self._client_lock = asyncio.Lock()

    def _server_rows(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM servers"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        return [dict(row) for row in db.query_all(sql)]

    async def get_client(self, server_id: int) -> tuple[dict[str, Any], BaseMcpClient]:
        async with self._client_lock:
            row = db.query_one("SELECT * FROM servers WHERE id=?", (server_id,))
            if not row:
                raise McpError(f"MCP 服务 {server_id} 不存在", -32001)
            server = dict(row)
            client = self._clients.get(server_id)
            if client is None:
                client = create_client(server)
                self._clients[server_id] = client
            return server, client

    async def invalidate(self, server_id: int | None = None) -> None:
        async with self._client_lock:
            if server_id is None:
                clients = list(self._clients.values())
                self._clients.clear()
            else:
                clients = [self._clients.pop(server_id)] if server_id in self._clients else []
        for client in clients:
            await client.close()
        async with self._cache_lock:
            self._cache = []
            self._mapping = {}
            self._cache_time = 0.0

    async def close(self) -> None:
        await self.invalidate()

    async def list_tools_for_server(self, server_id: int, refresh: bool = False) -> list[dict[str, Any]]:
        _, client = await self.get_client(server_id)
        if refresh:
            await self.invalidate(server_id)
            _, client = await self.get_client(server_id)
        return await client.list_tools()

    async def test_server(self, server_id: int) -> dict[str, Any]:
        server, client = await self.get_client(server_id)
        started = time.monotonic()
        await client.initialize()
        tools = await client.list_tools()
        return {
            "id": server_id,
            "name": server["name"],
            "transport": server["transport"],
            "ok": True,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "tool_count": len(tools),
            "tools": [tool.get("name") for tool in tools],
        }

    def _is_allowed(self, service_id: int, allowed_services: Any) -> bool:
        if allowed_services in (None, "*", ["*"]):
            return True
        return str(service_id) in {str(item) for item in allowed_services}

    async def refresh_tools(self, force: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        ttl = float(db.get_setting("tool_cache_ttl", "60") or 60)
        now = time.monotonic()
        if not force and self._cache and now - self._cache_time < ttl:
            return self._cache, self._mapping

        async with self._cache_lock:
            if not force and self._cache and time.monotonic() - self._cache_time < ttl:
                return self._cache, self._mapping
            aggregated: list[dict[str, Any]] = []
            mapping: dict[str, dict[str, Any]] = {}
            separator = db.get_setting("tool_name_separator", "__") or "__"
            for server in self._server_rows(enabled_only=True):
                service_id = int(server["id"])
                try:
                    _, client = await self.get_client(service_id)
                    tools = await client.list_tools()
                except Exception as exc:
                    logger.warning("获取服务 %s 的工具失败: %s", server["name"], exc)
                    continue
                prefix = f"{slugify(server['name'])}{separator}"
                for tool in tools:
                    upstream_name = str(tool.get("name", ""))
                    if not upstream_name:
                        continue
                    external_name = f"{prefix}{slugify(upstream_name)}"
                    suffix = 2
                    while external_name in mapping:
                        external_name = f"{prefix}{slugify(upstream_name)}_{suffix}"
                        suffix += 1
                    description = tool.get("description") or ""
                    if description:
                        description = f"[{server['name']}] {description}"
                    else:
                        description = f"来自 MCP 服务 {server['name']} 的工具 {upstream_name}"
                    aggregated.append(
                        {
                            "name": external_name,
                            "description": description,
                            "inputSchema": tool.get("inputSchema")
                            or {"type": "object", "properties": {}, "additionalProperties": True},
                            "_meta": {
                                "serviceId": service_id,
                                "serviceName": server["name"],
                                "upstreamName": upstream_name,
                            },
                        }
                    )
                    mapping[external_name] = {
                        "service_id": service_id,
                        "service_name": server["name"],
                        "tool_name": upstream_name,
                    }
            self._cache = aggregated
            self._mapping = mapping
            self._cache_time = time.monotonic()
            return self._cache, self._mapping

    async def list_tools(
        self, allowed_services: Any = "*", refresh: bool = False
    ) -> list[dict[str, Any]]:
        tools, _ = await self.refresh_tools(force=refresh)
        result = []
        for tool in tools:
            if self._is_allowed(int(tool["_meta"]["serviceId"]), allowed_services):
                public = {key: value for key, value in tool.items() if key != "_meta"}
                result.append(public)
        return result

    def tools_by_server(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in self._cache:
            meta = tool.get("_meta") or {}
            service_id = str(meta.get("serviceId") or "")
            if not service_id:
                continue
            grouped.setdefault(service_id, []).append(
                {
                    "name": meta.get("upstreamName"),
                    "displayName": tool.get("name"),
                    "description": tool.get("description"),
                }
            )
        return grouped

    async def resolve_tool(self, name: str, allowed_services: Any = "*") -> dict[str, Any]:
        _, mapping = await self.refresh_tools()
        info = mapping.get(name)
        if not info:
            _, mapping = await self.refresh_tools(force=True)
            info = mapping.get(name)
        if not info:
            raise McpError(f"未知工具: {name}", -32602)
        if not self._is_allowed(int(info["service_id"]), allowed_services):
            raise McpError(f"没有权限调用服务 {info['service_name']} 的工具", -32001)
        return info

    async def call_tool(
        self, name: str, arguments: dict[str, Any], allowed_services: Any = "*"
    ) -> tuple[Any, dict[str, Any]]:
        info = await self.resolve_tool(name, allowed_services)
        _, client = await self.get_client(int(info["service_id"]))
        result = await client.call_tool(info["tool_name"], arguments or {})
        return result, info

    def stats(self) -> dict[str, Any]:
        return {
            "activeClients": len(self._clients),
            "cachedTools": len(self._cache),
            "cacheAgeSeconds": round(time.monotonic() - self._cache_time, 1) if self._cache_time else None,
        }


def as_dict(row: Any) -> dict[str, Any]:
    value = dict(row)
    for key in ("args", "env", "headers"):
        if isinstance(value.get(key), str):
            try:
                value[key] = json.loads(value[key])
            except ValueError:
                value[key] = [] if key == "args" else {}
    value["enabled"] = bool(value.get("enabled"))
    return value
