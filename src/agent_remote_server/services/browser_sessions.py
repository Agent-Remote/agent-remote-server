import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, BrowserSession, Node, NodeTask, ToolAccount, User
from agent_remote_server.repositories.browser_sessions import BrowserSessionRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.security import create_opaque_token, hash_token
from agent_remote_server.services.tool_accounts import ACTIVE_NODE_STATUSES

DEFAULT_BROWSER_TARGET_URL = "https://claude.ai"
DEFAULT_BROWSER_IMAGE = "kasmweb/chrome:1.18.0"
DEFAULT_BROWSER_TTL_SECONDS = 1800
DEFAULT_BROWSER_VIEWPORT = {"width": 1440, "height": 900}
ACTIVE_BROWSER_STATUSES = {"starting", "ready"}


@dataclass(frozen=True)
class BrowserConnectInfo:
    """
    远端浏览器内嵌连接信息
    """

    browser_session_id: UUID
    status: str
    embed_url: str
    expires_at: datetime


class BrowserSessionService:
    """
    远端临时浏览器 session 生命周期服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = BrowserSessionRepository(session)
        self._identity_repository = IdentityRepository(session)

    async def list_browser_sessions(self, *, user: User) -> list[BrowserSession]:
        """
        列出用户浏览器 session，并标记已过期 session

        :param user (User): 当前用户
        :return list: 浏览器 session 列表
        """

        await self.expire_due_sessions()
        return list(await self._repository.list_browser_sessions_for_user(user.id))

    async def get_browser_session(self, *, user: User, browser_session_id: UUID) -> BrowserSession:
        """
        读取当前用户浏览器 session

        :param user (User): 当前用户
        :param browser_session_id (UUID): 浏览器 session ID
        :return BrowserSession: 浏览器 session 实体
        """

        await self.expire_due_sessions()
        return await self._require_user_browser_session(
            user=user, browser_session_id=browser_session_id
        )

    async def create_browser_session(
        self,
        *,
        user: User,
        tool_account_id: UUID | None,
        target_url: str | None,
        region_code: str | None,
        timezone: str | None,
        locale: str | None,
        ttl_seconds: int,
    ) -> BrowserSession:
        """
        创建远端临时浏览器 session 并投递节点任务

        :param user (User): 当前用户
        :param tool_account_id (UUID): 工具账户 ID
        :param target_url (str): 初始 URL
        :param region_code (str): 地区代码
        :param timezone (str): 时区
        :param locale (str): locale
        :param ttl_seconds (int): TTL 秒数
        :return BrowserSession: 浏览器 session 实体
        """

        ttl = ttl_seconds or DEFAULT_BROWSER_TTL_SECONDS
        if ttl < 60 or ttl > 7200:
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="ttl_seconds must be between 60 and 7200.",
                status_code=422,
            )
        account: ToolAccount | None = None
        preferred_tags: list[str] = []
        if tool_account_id is not None:
            account = await self._require_active_account(user=user, account_id=tool_account_id)
            region_code = account.region_code
            timezone = account.timezone
            locale = account.locale
            preferred_tags = list(account.preferred_node_tags)
        if not region_code or not timezone or not locale:
            raise ApiError(
                code="BROWSER_SESSION_CONFIG_REQUIRED",
                message="region_code, timezone, and locale are required without a tool account.",
                status_code=422,
            )
        node = await self._choose_browser_node(
            account=account,
            region_code=region_code,
            preferred_tags=preferred_tags,
        )
        now = self._now()
        browser_session = await self._repository.add_browser_session(
            BrowserSession(
                user_id=user.id,
                tool_account_id=account.id if account is not None else None,
                node_id=node.id,
                status="starting",
                region_code=region_code,
                timezone=timezone,
                locale=locale,
                target_url=target_url or DEFAULT_BROWSER_TARGET_URL,
                container_id=None,
                stream_endpoint=None,
                ttl_seconds=ttl,
                expires_at=now + timedelta(seconds=ttl),
                stopped_at=None,
            )
        )
        container_name = self._container_name(browser_session)
        browser_session.container_id = container_name
        task_id = f"create_browser_session:{browser_session.id}"
        await self._repository.add_task(
            NodeTask(
                node_id=node.id,
                task_id=task_id,
                task_type="create_browser_session",
                status="pending",
                payload={
                    "browser_session_id": str(browser_session.id),
                    "user_id": str(user.id),
                    "tool_account_id": str(account.id) if account is not None else None,
                    "target_url": browser_session.target_url,
                    "region_code": region_code,
                    "timezone": timezone,
                    "locale": locale,
                    "ttl_seconds": ttl,
                    "container_name": container_name,
                    "browser": {
                        "image": DEFAULT_BROWSER_IMAGE,
                        "engine": "chromium",
                        "mode": "incognito",
                        "viewport": DEFAULT_BROWSER_VIEWPORT,
                    },
                    "network_policy": {
                        "egress": "node_default",
                        "deny_private_networks": True,
                        "deny_metadata_service": True,
                        "disable_webrtc_local_ip": True,
                    },
                },
                retry_count=0,
            )
        )
        await self._audit(
            actor_user_id=user.id,
            action="browser_sessions.create",
            target_type="browser_session",
            target_id=str(browser_session.id),
            details={"node_id": str(node.id), "task_id": task_id},
        )
        await self._session.commit()
        return browser_session

    async def connect_info(self, *, user: User, browser_session_id: UUID) -> BrowserConnectInfo:
        """
        签发当前用户浏览器 session 的短期内嵌连接信息

        :param user (User): 当前用户
        :param browser_session_id (UUID): 浏览器 session ID
        :return BrowserConnectInfo: 内嵌连接信息
        """

        browser_session = await self.get_browser_session(
            user=user, browser_session_id=browser_session_id
        )
        if browser_session.status != "ready":
            raise ApiError(
                code="BROWSER_SESSION_NOT_READY",
                message="Browser session is not ready.",
                status_code=409,
            )
        token_expires_at = min(
            self._as_aware(browser_session.expires_at),
            self._now() + timedelta(minutes=5),
        )
        token = create_opaque_token("bembed")
        await self._store_embed_token(
            token=token,
            user_id=user.id,
            browser_session=browser_session,
            expires_at=token_expires_at,
        )
        return BrowserConnectInfo(
            browser_session_id=browser_session.id,
            status="ready",
            embed_url=f"/api/v1/browser-sessions/{browser_session.id}/stream?token={token}",
            expires_at=token_expires_at,
        )

    async def stream_html(self, *, browser_session_id: UUID, token: str) -> str:
        """
        校验短期 token 并返回内嵌浏览器 HTML

        :param browser_session_id (UUID): 浏览器 session ID
        :param token (str): 短期连接 token
        :return str: HTML 内容
        """

        endpoint = await self.stream_endpoint(browser_session_id=browser_session_id, token=token)
        if endpoint is None:
            return self._stream_unavailable_html()
        return self._stream_redirect_html(endpoint)

    async def stream_endpoint(self, *, browser_session_id: UUID, token: str) -> str | None:
        """
        校验短期 token 并返回节点浏览器代理 endpoint

        :param browser_session_id (UUID): 浏览器 session ID
        :param token (str): 短期连接 token
        :return str | None: 节点浏览器 endpoint
        """

        value = await self._load_embed_token(browser_session_id=browser_session_id, token=token)
        endpoint = value.get("stream_endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            return None
        return endpoint

    async def stop_browser_session(
        self, *, user: User, browser_session_id: UUID, reason: str = "user_requested"
    ) -> BrowserSession:
        """
        停止远端临时浏览器 session

        :param user (User): 当前用户
        :param browser_session_id (UUID): 浏览器 session ID
        :param reason (str): 停止原因
        :return BrowserSession: 浏览器 session 实体
        """

        browser_session = await self._require_user_browser_session(
            user=user, browser_session_id=browser_session_id
        )
        if browser_session.status in {"stopped", "failed", "expired"}:
            return browser_session
        await self._schedule_stop(browser_session=browser_session, reason=reason)
        browser_session.status = "stopping"
        await self._audit(
            actor_user_id=user.id,
            action="browser_sessions.stop",
            target_type="browser_session",
            target_id=str(browser_session.id),
            details={"reason": reason},
        )
        await self._session.commit()
        return browser_session

    async def delete_browser_session(self, *, user: User, browser_session_id: UUID) -> None:
        """
        删除已进入终态的浏览器 session

        :param user (User): 当前用户
        :param browser_session_id (UUID): 浏览器 session ID
        """

        browser_session = await self._require_user_browser_session(
            user=user, browser_session_id=browser_session_id
        )
        if browser_session.status not in {"stopped", "failed", "expired"}:
            raise ApiError(
                code="BROWSER_SESSION_DELETE_REQUIRES_STOPPED",
                message="Stop the browser session before deleting it.",
                status_code=409,
            )
        await self._audit(
            actor_user_id=user.id,
            action="browser_sessions.delete",
            target_type="browser_session",
            target_id=str(browser_session.id),
            details={"status": browser_session.status},
        )
        await self._repository.delete_browser_session(browser_session)
        await self._session.commit()

    async def expire_due_sessions(self) -> None:
        """
        标记已过期浏览器 session 并投递停止任务
        """

        expired = list(await self._repository.list_expired_active_sessions(self._now()))
        if not expired:
            return
        for browser_session in expired:
            browser_session.status = "expired"
            browser_session.stopped_at = self._now()
            await self._schedule_stop(browser_session=browser_session, reason="ttl_expired")
        await self._session.commit()

    async def _require_user_browser_session(
        self, *, user: User, browser_session_id: UUID
    ) -> BrowserSession:
        browser_session = await self._repository.get_browser_session(browser_session_id)
        if browser_session is None or browser_session.user_id != user.id:
            raise ApiError(
                code="BROWSER_SESSION_NOT_FOUND",
                message="Browser session was not found.",
                status_code=404,
            )
        return browser_session

    async def _require_active_account(self, *, user: User, account_id: UUID) -> ToolAccount:
        account = await self._repository.get_account(account_id)
        if account is None or account.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Tool account was not found.", status_code=404
            )
        if account.status != "active":
            raise ApiError(
                code="TOOL_ACCOUNT_NOT_ACTIVE",
                message="Tool account is not active.",
                status_code=409,
            )
        return account

    async def _choose_browser_node(
        self, *, account: ToolAccount | None, region_code: str, preferred_tags: list[str]
    ) -> Node:
        if account is not None and account.affinity_node_id is not None:
            node = await self._repository.get_node(account.affinity_node_id)
            if node is not None and self._node_can_host(node, region_code):
                return node
        candidates = await self._repository.list_candidate_nodes(
            region_code=region_code, preferred_tags=preferred_tags
        )
        if not candidates:
            raise ApiError(
                code="NODE_UNAVAILABLE",
                message="No available node can host this browser session.",
                status_code=409,
            )
        return candidates[0]

    def _node_can_host(self, node: Node | None, region_code: str) -> bool:
        if node is None or node.status not in ACTIVE_NODE_STATUSES:
            return False
        return node.region_code == region_code

    async def _schedule_stop(self, *, browser_session: BrowserSession, reason: str) -> None:
        task_id = f"stop_browser_session:{browser_session.id}"
        if await self._repository.get_task_by_task_id(task_id) is not None:
            return
        await self._repository.add_task(
            NodeTask(
                node_id=browser_session.node_id,
                task_id=task_id,
                task_type="stop_browser_session",
                status="pending",
                payload={
                    "browser_session_id": str(browser_session.id),
                    "container_name": browser_session.container_id,
                    "reason": reason,
                },
                retry_count=0,
            )
        )

    async def _store_embed_token(
        self,
        *,
        token: str,
        user_id: UUID,
        browser_session: BrowserSession,
        expires_at: datetime,
    ) -> None:
        ttl = max(1, int((expires_at - self._now()).total_seconds()))
        value = {
            "user_id": str(user_id),
            "browser_session_id": str(browser_session.id),
            "node_id": str(browser_session.node_id),
            "stream_endpoint": browser_session.stream_endpoint,
            "expires_at": expires_at.isoformat(),
        }
        client: Redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
        try:
            await client.setex(
                f"browser_embed:{hash_token(self._settings.secret_key, token)}",
                ttl,
                json.dumps(value, separators=(",", ":")),
            )
        except Exception:
            # Local tests and single-process demos may run without Redis. The opaque token
            # remains short-lived by URL contract; stream validation will require Redis.
            return
        finally:
            await client.aclose()

    async def _load_embed_token(self, *, browser_session_id: UUID, token: str) -> dict[str, object]:
        key = f"browser_embed:{hash_token(self._settings.secret_key, token)}"
        client: Redis = Redis.from_url(self._settings.redis_url, decode_responses=True)
        try:
            raw_value = await client.get(key)
        except Exception as exc:
            raise ApiError(
                code="BROWSER_SESSION_CONNECT_DENIED",
                message="Browser stream token validation is unavailable.",
                status_code=503,
            ) from exc
        finally:
            await client.aclose()
        if raw_value is None:
            raise ApiError(
                code="BROWSER_SESSION_CONNECT_DENIED",
                message="Browser stream token is invalid or expired.",
                status_code=403,
            )
        try:
            value: dict[str, object] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ApiError(
                code="BROWSER_SESSION_CONNECT_DENIED",
                message="Browser stream token is invalid.",
                status_code=403,
            ) from exc
        if value.get("browser_session_id") != str(browser_session_id):
            raise ApiError(
                code="BROWSER_SESSION_CONNECT_DENIED",
                message="Browser stream token does not match this session.",
                status_code=403,
            )
        return value

    def _container_name(self, browser_session: BrowserSession) -> str:
        return f"agent-remote-browser-{str(browser_session.id).replace('-', '')[:24]}"

    async def _audit(
        self,
        *,
        actor_user_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, object],
    ) -> None:
        await self._identity_repository.add_audit_log(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _as_aware(self, value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def _stream_redirect_html(self, endpoint: str) -> str:
        safe_endpoint = escape(endpoint, quote=True)
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="refresh" content="0; url={safe_endpoint}" />
    <style>
      html, body {{
        width: 100%;
        height: 100%;
        margin: 0;
        overflow: hidden;
        background: #0f1419;
        color: #dce3ea;
        font: 14px system-ui, sans-serif;
      }}
    </style>
    <script>
      window.location.replace("{safe_endpoint}");
    </script>
  </head>
  <body>Opening remote browser...</body>
</html>
"""

    def _stream_unavailable_html(self) -> str:
        return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body {
        margin: 0;
        display: grid;
        min-height: 100vh;
        place-items: center;
        background: #0f1419;
        color: #dce3ea;
        font: 14px system-ui, sans-serif;
      }
    </style>
  </head>
  <body>Remote browser stream endpoint is not externally routable.</body>
</html>
"""
