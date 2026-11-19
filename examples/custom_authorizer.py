from __future__ import annotations

from typing import Any


class AdvancedAuthorizer:
    """自定义授权示例。

    在管理后台“系统设置 -> 自定义 Authorizer”中填写：
    examples.custom_authorizer:AdvancedAuthorizer

    authorize(context) 可返回 True/False；返回 False 会拒绝本次操作。
    """

    BLOCKED_TOOLS = {"dangerous_tool"}

    async def authorize(self, context: dict[str, Any]) -> bool:
        principal = context.get("principal") or {}
        action = context.get("action")
        tool_name = context.get("tool_name")
        service_name = context.get("service_name")

        if tool_name in self.BLOCKED_TOOLS:
            return False
        if action == "tools:call" and service_name == "internal-debug" and "admin" not in principal.get("scopes", []):
            return False
        if context.get("request_path", "").startswith("/internal") and "admin" not in principal.get("scopes", []):
            return False
        return True


class WorkHoursAuthorizer:
    """仅允许指定 Token 名称在时间窗口内调用工具的示例。"""

    ALLOWED_NAMES = {"workbuddy"}

    def authorize(self, context: dict[str, Any]) -> bool:
        if context.get("action") != "tools:call":
            return True
        return (context.get("principal") or {}).get("name") in self.ALLOWED_NAMES
