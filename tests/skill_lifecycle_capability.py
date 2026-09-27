"""
在隔离验收请求内补充未发布的能力，避免用全局 HTTP 锁阻塞真实心跳。
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from test_skill_session_admission import capability


class LifecycleCapabilityMiddleware:
    """
    仅改写验收心跳中的技能能力，认证、健康探测和数据库提交仍使用生产路径。
    """

    def __init__(self, app: ASGIApp) -> None:
        """
        保存被包装的隔离应用。

        :param app (ASGIApp): 原生产路由应用
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        将测试能力与真实心跳在同一事务中写入，不阻塞其他路由。

        :param scope (Scope): 当前 ASGI 请求范围
        :param receive (Receive): 原消息接收函数
        :param send (Send): 原响应发送函数
        """
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != "/api/v1/node-api/heartbeat"
        ):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            assert message["type"] == "http.request"
            body.extend(message.get("body", b""))
            assert len(body) <= 64 << 10, "disposable heartbeat exceeded fixture bound"
            if not message.get("more_body", False):
                break
        payload = json.loads(body)
        original = payload["runtime"]["runtime_capabilities"]
        assert "skill_manager" not in original, "production advertisement changed"
        original["skill_manager"] = {"native": capability() | {"deployment_protocol_version": 1}}
        encoded = json.dumps(payload).encode()
        forwarded = False

        async def amended_receive() -> Message:
            """
            只替换一次完整请求体，后续断连观察仍来自原连接。

            :return Message: 改写后的消息或原连接的后续消息
            """
            nonlocal forwarded
            if not forwarded:
                forwarded = True
                return {"type": "http.request", "body": encoded, "more_body": False}
            return await receive()

        scope["headers"] = [
            (name, value) for name, value in scope["headers"] if name.lower() != b"content-length"
        ] + [(b"content-length", str(len(encoded)).encode())]
        await self.app(scope, amended_receive, send)
