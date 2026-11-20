from __future__ import annotations

import json
import sys
import uuid

from fastapi.testclient import TestClient

from app import db
from app.config import BASE_DIR
from app.main import app


def test_login_token_mcp_and_websocket():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert login.status_code == 200, login.text

        client.put("/api/settings", json={"values": {"auth_authorizer": ""}})
        created = client.post(
            "/api/tokens",
            json={
                "name": "smoke-test",
                "scopes": ["mcp:read", "mcp:call", "ws:connect"],
                "allowed_services": "*",
            },
        )
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        token_id = created.json()["id"]
        assert created.json()["prefixFull"].startswith("mcp-open_")

        try:
            overview = client.get("/api/overview").json()
            assert isinstance(overview["xiaozhiBridge"], list)

            headers = {"Authorization": f"Bearer {token}"}
            response = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200, response.text
            assert response.headers.get("mcp-session-id")
            assert "tools" in response.json()["result"]

            with client.websocket_connect(f"/ws/xiaozhi?token={token}") as websocket:
                hello = websocket.receive_json()
                assert hello["type"] == "hello"
                websocket.send_json({"type": "ping", "id": "ping-1"})
                assert websocket.receive_json()["type"] == "pong"

            regenerated = client.post(f"/api/tokens/{token_id}/regenerate")
            assert regenerated.status_code == 200, regenerated.text
            new_token = regenerated.json()["token"]
            assert new_token != token
            payload = {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}
            assert client.post("/mcp", headers=headers, json=payload).status_code == 401
            assert client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {new_token}"},
                json=payload,
            ).status_code == 200
        finally:
            client.delete(f"/api/tokens/{token_id}")


def test_bridge_crud_and_masking():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200
        name = f"bridge-{uuid.uuid4().hex[:8]}"
        server_name = f"bridge-target-{uuid.uuid4().hex[:8]}"
        now = db.utc_now()
        server_id = db.execute(
            "INSERT INTO servers(name, transport, command, args, cwd, env, url, headers, enabled, timeout, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                server_name,
                "http",
                None,
                "[]",
                None,
                "{}",
                "http://example.invalid/mcp",
                "{}",
                0,
                5,
                now,
                now,
            ),
        )
        created = client.post(
            "/api/bridges",
            json={
                "name": name,
                "url": "wss://example.invalid/mcp/?token=super-secret",
                "enabled": False,
                "allowed_services": [server_id],
            },
        )
        assert created.status_code == 201, created.text
        data = created.json()
        assert "super-secret" not in data["displayUrl"]
        assert data["connected"] is False
        assert server_name in data["allowed_service_names"]

        try:
            rows = client.get("/api/bridges").json()
            assert any(row["id"] == data["id"] for row in rows)
            updated = client.put(
                f"/api/bridges/{data['id']}",
                json={"name": f"{name}-updated", "enabled": False, "allowed_services": "*"},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["name"] == f"{name}-updated"
            assert updated.json()["allowed_service_names"] == ["全部服务"]
        finally:
            assert client.delete(f"/api/bridges/{data['id']}").status_code == 200
            db.execute("DELETE FROM servers WHERE id=?", (server_id,))


def test_server_list_includes_capabilities():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200
        name = f"cap-test-{uuid.uuid4().hex[:8]}"
        script = BASE_DIR / "examples" / "echo_server.py"
        server_id = db.execute(
            "INSERT INTO servers(name, transport, command, args, cwd, env, url, headers, enabled, timeout, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                "stdio",
                sys.executable,
                json.dumps([str(script)]),
                str(BASE_DIR),
                "{}",
                None,
                "{}",
                1,
                15,
                db.utc_now(),
                db.utc_now(),
            ),
        )
        token_id = None
        try:
            rows = client.get("/api/servers").json()
            row = next(item for item in rows if item["id"] == server_id)
            assert row["toolCount"] >= 3
            names = {tool["name"] for tool in row["tools"]}
            assert {"echo", "add", "now"}.issubset(names)

            created_token = client.post(
                "/api/tokens",
                json={
                    "name": f"limited-{uuid.uuid4().hex[:6]}",
                    "scopes": ["mcp:read", "mcp:call"],
                    "allowed_services": [server_id],
                },
            )
            assert created_token.status_code == 201, created_token.text
            token_id = created_token.json()["id"]
            assert name in created_token.json()["allowed_service_names"]
        finally:
            if token_id:
                client.delete(f"/api/tokens/{token_id}")
            db.execute("DELETE FROM servers WHERE id=?", (server_id,))
