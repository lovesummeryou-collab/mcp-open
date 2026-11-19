from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any


def ok(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


TOOLS = [
    {
        "name": "echo",
        "description": "回显输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "计算两个数字的和",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "now",
        "description": "返回当前 Unix 时间戳",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        text = str(arguments.get("text", ""))
        return {"content": [{"type": "text", "text": text}]}
    if name == "add":
        try:
            value = float(arguments["a"]) + float(arguments["b"])
        except (KeyError, TypeError, ValueError) as exc:
            return {"isError": True, "content": [{"type": "text", "text": f"参数错误: {exc}"}]}
        return {"content": [{"type": "text", "text": str(value)}], "structuredContent": {"result": value}}
    if name == "now":
        return {"content": [{"type": "text", "text": str(int(time.time()))}]}
    return {"isError": True, "content": [{"type": "text", "text": f"未知工具: {name}"}]}


async def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return ok(
            message_id,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "echo-mcp-server", "version": "0.1.0"},
            },
        )
    if method == "ping":
        return ok(message_id, {})
    if method == "tools/list":
        return ok(message_id, {"tools": TOOLS})
    if method == "tools/call":
        return ok(message_id, call_tool(str(params.get("name", "")), params.get("arguments") or {}))
    if method and method.startswith("notifications/"):
        return None
    if message_id is not None:
        return error(message_id, -32601, f"不支持的方法: {method}")
    return None


async def main() -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        try:
            message = json.loads(line)
            response = await handle(message)
        except ValueError:
            continue
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
