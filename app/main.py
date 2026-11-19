from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db
from .config import CORS_ORIGINS, DEBUG, STATIC_DIR
from .gateway import handle_jsonrpc, rpc_error
from .registry import ServerRegistry
from .xiaozhi import websocket_gateway
from .xiaozhi_bridge import XiaozhiBridge

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp-open")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    app.state.registry = ServerRegistry()
    app.state.sse_sessions: dict[str, asyncio.Queue] = {}
    app.state.bridge = XiaozhiBridge(app.state.registry)
    await app.state.bridge.reload()
    logger.info("mcp-open 已启动")
    yield
    await app.state.bridge.stop()
    await app.state.registry.close()
    app.state.sse_sessions.clear()
    logger.info("mcp-open 已停止")


app = FastAPI(
    title="mcp-open",
    description="本地/HTTP MCP 服务注册、聚合、鉴权与小智 WebSocket 网关",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "MCP-Protocol-Version"],
)

from .api import router as api_router  # noqa: E402

app.include_router(api_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _mcp_headers(session_id: str) -> dict[str, str]:
    return {
        "Mcp-Session-Id": session_id,
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": "Mcp-Session-Id",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "mcp-open"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/mcp")
async def mcp_streamable_http(request: Request) -> Response:
    principal = auth.require_api_token(request)
    session_id = request.headers.get("mcp-session-id") or str(uuid.uuid4())
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            rpc_error(None, -32700, "JSON 解析失败"),
            status_code=400,
            headers=_mcp_headers(session_id),
        )
    result = await handle_jsonrpc(payload, principal, request.app.state.registry, request=request)
    if result is None:
        return Response(status_code=202, headers=_mcp_headers(session_id))
    return JSONResponse(result, headers=_mcp_headers(session_id))


@app.get("/mcp")
async def mcp_sse_stream(request: Request) -> StreamingResponse:
    auth.require_api_token(request)

    async def events():
        yield ": mcp-open streamable http session\n\n"
        while not await request.is_disconnected():
            await asyncio.sleep(15)
            yield ": keepalive\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=_mcp_headers(str(uuid.uuid4())),
    )


@app.delete("/mcp")
async def mcp_session_close(request: Request) -> Response:
    auth.require_api_token(request)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@app.get("/sse")
async def mcp_legacy_sse(request: Request) -> StreamingResponse:
    auth.require_api_token(request)
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    request.app.state.sse_sessions[session_id] = queue
    token = auth._extract_token(request)
    endpoint = f"/messages/?session_id={session_id}"
    if token:
        endpoint += f"&token={quote(token)}"

    async def events():
        try:
            yield f"event: endpoint\ndata: {endpoint}\n\n"
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        finally:
            request.app.state.sse_sessions.pop(session_id, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Expose-Headers": "Mcp-Session-Id",
        },
    )


@app.post("/messages/")
async def mcp_legacy_messages(request: Request) -> Response:
    principal = auth.require_api_token(request)
    session_id = request.query_params.get("session_id")
    if not session_id or session_id not in request.app.state.sse_sessions:
        raise HTTPException(status_code=404, detail="SSE 会话不存在或已过期")
    try:
        payload = await request.json()
    except ValueError:
        return Response(status_code=400)
    result = await handle_jsonrpc(payload, principal, request.app.state.registry, request=request)
    if result is not None:
        await request.app.state.sse_sessions[session_id].put(result)
    return Response(status_code=202)


@app.websocket("/ws/xiaozhi")
@app.websocket("/ws/mcp")
async def xiaozhi_websocket(websocket: WebSocket) -> None:
    await websocket_gateway(websocket, websocket.app.state.registry)
