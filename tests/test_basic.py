from __future__ import annotations

import asyncio

from app import db
from app.auth import Principal
from app.gateway import handle_jsonrpc
from app.registry import slugify
from app.security import hash_password, verify_password
from app.xiaozhi_bridge import redact_url


class FakeRegistry:
    def __init__(self) -> None:
        self.tools = [
            {
                "name": "echo__echo",
                "description": "回显",
                "inputSchema": {"type": "object", "properties": {}},
                "_meta": {"serviceId": 1, "serviceName": "echo", "upstreamName": "echo"},
            }
        ]

    async def list_tools(self, allowed_services="*"):
        if allowed_services in (None, "*", ["*"]):
            return [{key: value for key, value in tool.items() if key != "_meta"} for tool in self.tools]
        return []

    async def resolve_tool(self, name, allowed_services="*"):
        return {"service_id": 1, "service_name": "echo", "tool_name": "echo"}

    async def call_tool(self, name, arguments, allowed_services="*"):
        return {"content": [{"type": "text", "text": arguments.get("text", "")}]}, {
            "service_id": 1,
            "service_name": "echo",
            "tool_name": "echo",
        }


def test_password_hash_and_verify():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)


def test_slugify():
    assert slugify("My MCP Server") == "My_MCP_Server"
    assert slugify("中文服务") == "server"


def test_gateway_initialize_and_tools_call():
    db.init_db()
    db.set_settings({"auth_authorizer": ""})
    principal = Principal(1, "test", {"mcp:read", "mcp:call"}, "*")
    registry = FakeRegistry()

    initialize = asyncio.run(
        handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            principal,
            registry,
        )
    )
    assert initialize["result"]["serverInfo"]["name"] == "mcp-open"

    tools = asyncio.run(
        handle_jsonrpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, principal, registry)
    )
    assert tools["result"]["tools"][0]["name"] == "echo__echo"

    called = asyncio.run(
        handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo__echo", "arguments": {"text": "hello"}},
            },
            principal,
            registry,
        )
    )
    assert called["result"]["content"][0]["text"] == "hello"


def test_gateway_denies_token_by_service_scope():
    db.init_db()
    db.set_settings({"auth_authorizer": ""})
    principal = Principal(2, "limited", {"mcp:call"}, ["999"])
    result = asyncio.run(
        handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo__echo", "arguments": {}},
            },
            principal,
            FakeRegistry(),
        )
    )
    assert "error" in result
    assert result["error"]["code"] == -32001


def test_redact_url_hides_token():
    value = redact_url("wss://api.xiaozhi.me/mcp/?token=super-secret-value")
    assert "super-secret-value" not in value
    assert "token=***" in value
