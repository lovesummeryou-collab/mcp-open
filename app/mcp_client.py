from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .config import PROTOCOL_VERSION, SERVER_NAME

logger = logging.getLogger("mcp-open.mcp-client")
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpError(RuntimeError):
    def __init__(self, message: str, code: int = -32603, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def _parse_json_fields(server: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(server)

    def loads(value: Any, default: Any) -> Any:
        if value is None or value == "":
            return default
        if isinstance(value, (list, dict)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    parsed["args"] = loads(server.get("args"), [])
    parsed["env"] = loads(server.get("env"), {})
    parsed["headers"] = loads(server.get("headers"), {})
    return parsed


class BaseMcpClient(ABC):
    def __init__(self, server: dict[str, Any]):
        self.server = _parse_json_fields(server)
        self.server_id = int(server["id"])
        self.name = str(server["name"])
        self.timeout = float(server.get("timeout") or 30)
        self._request_id = 0
        self._init_lock = asyncio.Lock()
        self._initialized = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _prepare(self) -> None:
        return None

    @abstractmethod
    async def _send_request(self, message: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def _send_notification(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._prepare()
            result = await self._send_request(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                    },
                }
            )
            self.protocol_version = result.get("protocolVersion") if isinstance(result, dict) else PROTOCOL_VERSION
            await self._send_notification({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            self._initialized = True

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self.initialize()
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        return await self._send_request(message)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.initialize()
        await self._send_notification({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {})
        return list(result.get("tools", [])) if isinstance(result, dict) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})

    async def close(self) -> None:
        return None

    @staticmethod
    def _raise_for_error(message: dict[str, Any]) -> None:
        if "error" in message:
            error = message["error"] or {}
            raise McpError(
                str(error.get("message", "MCP 服务返回错误")),
                int(error.get("code", -32603)),
                error.get("data"),
            )


class StdioMcpClient(BaseMcpClient):
    def __init__(self, server: dict[str, Any]):
        super().__init__(server)
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False

    async def _prepare(self) -> None:
        if self.process and self.process.returncode is None:
            return
        command = expand_env(self.server.get("command"))
        args = [str(item) for item in expand_env(self.server.get("args") or [])]
        if not command:
            raise McpError(f"服务 {self.name} 未配置启动命令")
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in (expand_env(self.server.get("env") or {})).items()})
        cwd = expand_env(self.server.get("cwd")) or None
        logger.info("启动本地 MCP 服务 %s: %s %s", self.name, command, " ".join(args))
        self.process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(), name=f"mcp-stdout-{self.server_id}")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"mcp-stderr-{self.server_id}")

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                logger.debug("[%s] 非 JSON 输出: %s", self.name, line[:200])
                continue
            message_id = message.get("id")
            if message_id in self._pending:
                future = self._pending.pop(message_id)
                if not future.done():
                    future.set_result(message)
        await self._fail_pending("本地 MCP 服务已退出")
        self.process = None

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            logger.info("[%s stderr] %s", self.name, line.decode("utf-8", errors="replace").rstrip())

    async def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(reason))
        self._pending.clear()

    async def _send_line(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            raise McpError(f"本地 MCP 服务 {self.name} 未运行")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def _send_request(self, message: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = future
        try:
            await self._send_line(message)
            response = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(message["id"], None)
            raise McpError(f"调用本地 MCP 服务 {self.name} 超时", -32000) from exc
        finally:
            self._pending.pop(message["id"], None)
        self._raise_for_error(response)
        return response.get("result", {})

    async def _send_notification(self, message: dict[str, Any]) -> None:
        await self._send_line(message)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._fail_pending("连接已关闭")
        process = self.process
        self.process = None
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()


class HttpMcpClient(BaseMcpClient):
    def __init__(self, server: dict[str, Any], transport: str = "http"):
        super().__init__(server)
        self.transport = transport
        self.url = str(expand_env(server.get("url") or ""))
        self.headers = {str(key): str(value) for key, value in (expand_env(self.server.get("headers") or {})).items()}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, read=self.timeout), follow_redirects=True)
        self._session_id: str | None = None
        self._closed = False
        self._pending: dict[int, asyncio.Future] = {}
        self._message_url: str | None = None
        self._stream_client: httpx.AsyncClient | None = None
        self._stream_response: httpx.Response | None = None
        self._stream_task: asyncio.Task | None = None
        self._endpoint_ready = asyncio.Event()

    async def _prepare(self) -> None:
        if self.transport == "sse":
            await self._open_sse_stream()

    def _base_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update(self.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _decode_sse(text: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        data_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    try:
                        messages.append(json.loads(payload))
                    except ValueError:
                        pass
                    data_lines = []
        if data_lines:
            try:
                messages.append(json.loads("\n".join(data_lines)))
            except ValueError:
                pass
        return messages

    def _find_response(self, messages: list[dict[str, Any]], message_id: int | None) -> dict[str, Any] | None:
        for message in messages:
            if message_id is not None and message.get("id") == message_id:
                return message
            if message_id is None and "method" not in message:
                return message
        return None

    async def _send_request(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.transport == "sse":
            return await self._send_request_sse(message)
        response = await self._client.post(self.url, json=message, headers=self._base_headers())
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code >= 400:
            raise McpError(f"HTTP MCP 服务 {self.name} 返回 {response.status_code}: {response.text[:300]}")
        content_type = response.headers.get("content-type", "")
        if not response.content:
            raise McpError(f"HTTP MCP 服务 {self.name} 返回空响应")
        messages: list[dict[str, Any]] = []
        if "text/event-stream" in content_type:
            messages = self._decode_sse(response.text)
        else:
            try:
                decoded = response.json()
                messages = decoded if isinstance(decoded, list) else [decoded]
            except ValueError as exc:
                raise McpError(f"HTTP MCP 服务 {self.name} 返回非法 JSON") from exc
        result_message = self._find_response(messages, message.get("id"))
        if not result_message:
            raise McpError(f"HTTP MCP 服务 {self.name} 未返回对应响应")
        self._raise_for_error(result_message)
        return result_message.get("result", {})

    async def _send_notification(self, message: dict[str, Any]) -> None:
        if self.transport == "sse":
            await self._send_notification_sse(message)
            return
        response = await self._client.post(self.url, json=message, headers=self._base_headers())
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code >= 400:
            raise McpError(f"HTTP MCP 服务 {self.name} 返回 {response.status_code}: {response.text[:300]}")

    async def _open_sse_stream(self) -> None:
        if self._stream_task and not self._stream_task.done() and self._endpoint_ready.is_set():
            return
        self._endpoint_ready.clear()
        self._stream_client = httpx.AsyncClient(timeout=None, follow_redirects=True)
        request = self._stream_client.build_request(
            "GET", self.url, headers={"Accept": "text/event-stream", **self.headers}
        )
        self._stream_response = await self._stream_client.send(request, stream=True)
        if self._stream_response.status_code >= 400:
            raise McpError(f"SSE MCP 服务 {self.name} 返回 {self._stream_response.status_code}")
        self._stream_task = asyncio.create_task(self._consume_sse(), name=f"mcp-sse-{self.server_id}")
        try:
            await asyncio.wait_for(self._endpoint_ready.wait(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(f"等待 SSE endpoint 超时: {self.name}") from exc

    async def _consume_sse(self) -> None:
        assert self._stream_response is not None
        data_lines: list[str] = []
        event_name = "message"
        try:
            async for line in self._stream_response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    if not data_lines:
                        continue
                    payload = "\n".join(data_lines)
                    data_lines = []
                    if event_name == "endpoint":
                        from urllib.parse import urljoin

                        self._message_url = urljoin(self.url, payload)
                        self._endpoint_ready.set()
                    else:
                        try:
                            message = json.loads(payload)
                        except ValueError:
                            continue
                        message_id = message.get("id")
                        if message_id in self._pending:
                            future = self._pending.pop(message_id)
                            if not future.done():
                                future.set_result(message)
                    event_name = "message"
        except Exception as exc:  # pragma: no cover - 网络异常由下一次请求发现
            logger.debug("SSE 连接中断 %s: %s", self.name, exc)
        finally:
            self._endpoint_ready.clear()
            await self._fail_pending("SSE 连接已断开")

    async def _post_sse(self, message: dict[str, Any]) -> None:
        if not self._message_url:
            raise McpError(f"SSE MCP 服务 {self.name} 尚未提供消息端点")
        response = await self._client.post(
            self._message_url, json=message, headers={"Content-Type": "application/json", **self.headers}
        )
        if response.status_code >= 400:
            raise McpError(f"SSE MCP 服务 {self.name} 返回 {response.status_code}: {response.text[:300]}")

    async def _send_request_sse(self, message: dict[str, Any]) -> dict[str, Any]:
        await self._open_sse_stream()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = future
        try:
            await self._post_sse(message)
            response = await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(f"调用 SSE MCP 服务 {self.name} 超时", -32000) from exc
        finally:
            self._pending.pop(message["id"], None)
        self._raise_for_error(response)
        return response.get("result", {})

    async def _send_notification_sse(self, message: dict[str, Any]) -> None:
        await self._open_sse_stream()
        await self._post_sse(message)

    async def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(reason))
        self._pending.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._fail_pending("连接已关闭")
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        if self._stream_response is not None:
            with contextlib.suppress(Exception):
                await self._stream_response.aclose()
        if self._stream_client is not None:
            with contextlib.suppress(Exception):
                await self._stream_client.aclose()
        await self._client.aclose()


def create_client(server: dict[str, Any]) -> BaseMcpClient:
    transport = str(server.get("transport") or "stdio")
    if transport == "stdio":
        return StdioMcpClient(server)
    if transport in {"http", "sse"}:
        return HttpMcpClient(server, transport)
    raise McpError(f"不支持的传输方式: {transport}")
