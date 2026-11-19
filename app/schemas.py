from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str
    password: str


class ServerBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    timeout: float = Field(default=30, gt=0, le=600)

    @model_validator(mode="after")
    def check_transport_fields(self) -> "ServerBase":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio 服务必须提供 command")
        if self.transport in {"http", "sse"} and not self.url:
            raise ValueError("http/sse 服务必须提供 url")
        return self


class ServerCreate(ServerBase):
    pass


class ServerUpdate(ServerBase):
    pass


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: ["mcp:read", "mcp:call"])
    allowed_services: str | list[int] | list[str] = "*"
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class TokenUpdate(BaseModel):
    name: str | None = None
    scopes: list[str] | None = None
    allowed_services: str | list[int] | list[str] | None = None
    is_active: bool | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=6, max_length=256)
    role: str = "admin"


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=256)
    role: str | None = None
    is_active: bool | None = None


class BridgeBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_ws_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("WebSocket 地址不能为空")
        if not (value.startswith(("ws://", "wss://")) or "${" in value):
            raise ValueError("地址必须以 ws:// 或 wss:// 开头")
        return value


class BridgeCreate(BridgeBase):
    pass


class BridgeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def validate_ws_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("WebSocket 地址不能为空")
        if not (value.startswith(("ws://", "wss://")) or "${" in value):
            raise ValueError("地址必须以 ws:// 或 wss:// 开头")
        return value


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class AuthorizerContext(BaseModel):
    principal: dict[str, Any]
    action: str
    service_id: int | None = None
    service_name: str | None = None
    tool_name: str | None = None
    request_path: str | None = None
    method: str | None = None
    client_ip: str | None = None
