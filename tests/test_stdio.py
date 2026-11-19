from __future__ import annotations

import asyncio
import json
import sys
import uuid

from app import db
from app.config import BASE_DIR
from app.registry import ServerRegistry


def test_stdio_server_discovers_tools():
    db.init_db()
    name = f"echo-test-{uuid.uuid4().hex[:8]}"
    script = BASE_DIR / "examples" / "echo_server.py"
    server_id = db.execute(
        "INSERT INTO servers(name, transport, command, args, cwd, env, url, headers, enabled, timeout, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, "stdio", sys.executable, json.dumps([str(script)]), str(BASE_DIR), "{}", None, "{}", 1, 15, db.utc_now(), db.utc_now()),
    )
    registry = ServerRegistry()

    async def run_check():
        try:
            result = await registry.test_server(int(server_id))
            assert result["ok"] is True, result
            assert result["tool_count"] >= 3
            assert "echo__echo" not in result["tools"]  # 上游工具名未加服务前缀
            assert "echo" in result["tools"]
        finally:
            await registry.close()

    try:
        asyncio.run(run_check())
    finally:
        db.execute("DELETE FROM servers WHERE id=?", (server_id,))
