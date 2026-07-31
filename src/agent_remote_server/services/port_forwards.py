from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, AuthToken, Node, PortForward, Session, User
from agent_remote_server.port_forward_tokens import (
    PortForwardTokenClaims,
    PortForwardTokenStore,
)
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.port_forwards import (
    NON_TERMINAL_FORWARD_STATUSES,
    PortForwardRepository,
)
from agent_remote_server.security import create_opaque_token, hash_token

TERMINAL_FORWARD_STATUSES = {"stopped", "expired", "revoked", "failed"}
RUNNING_SESSION_STATUSES = {"running", "active"}
ACTIVE_NODE_STATUSES = {"healthy", "degraded", "active"}


@dataclass(frozen=True)
class PortForwardPolicy:
    """
    解析后的端口转发策略
    """

    enabled: bool
    min_port: int
    max_port: int
    denied_ports: frozenset[int]
    max_per_user: int
    max_per_device: int
    max_per_session: int
    max_streams: int
    default_ttl_seconds: int
    max_ttl_seconds: int
    lease_seconds: int
    control_plane_grace_seconds: int
    bytes_per_second: int

    def snapshot(self) -> dict[str, object]:
        """
        返回不含敏感值的策略快照

        :return dict: 策略快照
        """

        return {
            "min_port": self.min_port,
            "max_port": self.max_port,
            "denied_ports": sorted(self.denied_ports),
            "max_streams": self.max_streams,
            "max_ttl_seconds": self.max_ttl_seconds,
            "lease_seconds": self.lease_seconds,
            "control_plane_grace_seconds": self.control_plane_grace_seconds,
            "bytes_per_second": self.bytes_per_second,
        }


@dataclass(frozen=True)
class IssuedPortForwardConnection:
    """
    已签发的一次性连接 token
    """

    token: str
    expires_at: datetime


@dataclass(frozen=True)
class CreatedPortForward:
    """
    新建端口转发结果
    """

    port_forward: PortForward
    node: Node
    connection: IssuedPortForwardConnection


@dataclass(frozen=True)
class RedeemedPortForward:
    """
    Node 已兑换端口转发授权
    """

    port_forward: PortForward
    tool_session: Session
    policy: PortForwardPolicy


@dataclass(frozen=True)
class PortForwardCleanupResult:
    """一次生命周期对账批次的结果。"""

    changed: int
    next_cursor: UUID | None


class PortForwardService:
    """
    Session 端口转发授权服务
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        token_store: PortForwardTokenStore,
    ) -> None:
        self._session = session
        self._settings = settings
        self._token_store = token_store
        self._repository = PortForwardRepository(session)
        self._identity_repository = IdentityRepository(session)

    async def create(
        self,
        *,
        user: User,
        token: AuthToken,
        session_id: UUID,
        remote_port: int,
        local_port: int,
        client_instance_id: str,
        ttl_seconds: int | None,
    ) -> CreatedPortForward:
        """
        创建受控 session 端口转发

        :param user (User): 当前用户
        :param token (AuthToken): 当前设备 token
        :param session_id (UUID): 工具 session ID
        :param remote_port (int): Runtime 远端端口
        :param local_port (int): 客户端请求的本地端口
        :param client_instance_id (str): CLI 实例 ID
        :param ttl_seconds (int): 请求有效秒数

        :return CreatedPortForward: 新建端口转发结果
        """

        device_id = self._require_device_token(token)
        await self._require_rate_limit(
            scope=f"create:{user.id}:{device_id}",
            limit=self._settings.port_forward_create_rate_limit_per_minute,
        )
        await self._repository.lock_user(user.id)
        device = await self._repository.get_active_device(user_id=user.id, device_id=device_id)
        if device is None:
            raise ApiError(code="DEVICE_REVOKED", message="Device is not active.", status_code=403)
        ssh_key = await self._repository.first_active_ssh_key(device.id)
        if ssh_key is None:
            raise ApiError(
                code="SSH_KEY_MISSING",
                message="Current device has no active SSH key.",
                status_code=409,
            )
        tool_session = await self._require_running_session(user.id, session_id)
        node = await self._require_node(tool_session.node_id)
        policy = self._policy(node)
        self._require_capability(node, tool_session)
        self._validate_remote_port(policy, remote_port)
        await self._validate_quotas(
            policy=policy,
            user_id=user.id,
            device_id=device.id,
            session_id=tool_session.id,
        )
        requested_ttl = ttl_seconds or policy.default_ttl_seconds
        if requested_ttl > policy.max_ttl_seconds:
            raise ApiError(
                code="POLICY_LIMIT",
                message="Requested port forward TTL exceeds policy.",
                status_code=409,
                details={"max_ttl_seconds": policy.max_ttl_seconds},
            )
        now = self._now()
        port_forward = await self._repository.add(
            PortForward(
                user_id=user.id,
                device_id=device.id,
                ssh_key_id=None,
                session_id=tool_session.id,
                node_id=node.id,
                remote_port=remote_port,
                requested_local_port=local_port,
                client_instance_id=client_instance_id,
                status="pending",
                policy_snapshot=policy.snapshot(),
                bytes_up=0,
                bytes_down=0,
                connection_count=0,
                connection_generation=0,
                expires_at=now + timedelta(seconds=requested_ttl),
            )
        )
        connection = await self._issue_connection(port_forward)
        await self._audit(
            actor_user_id=user.id,
            action="port_forward.created",
            port_forward=port_forward,
            details={
                "device_id": str(device.id),
                "session_id": str(tool_session.id),
                "node_id": str(node.id),
                "remote_port": remote_port,
                "requested_local_port": local_port,
            },
        )
        await self._session.commit()
        return CreatedPortForward(port_forward=port_forward, node=node, connection=connection)

    async def list(self, *, user: User, all_users: bool) -> list[PortForward]:
        """
        列出可见端口转发

        :param user (User): 当前用户
        :param all_users (bool): 是否列出全部用户

        :return list: 端口转发列表
        """

        if all_users and user.role != "admin":
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Administrator role is required.",
                status_code=403,
            )
        values = (
            await self._repository.list_all()
            if all_users
            else await self._repository.list_for_user(user.id)
        )
        changed = False
        for port_forward in values:
            changed = self._expire_if_needed(port_forward) or changed
        if changed:
            await self._session.commit()
        return list(values)

    async def get(self, *, user: User, forward_id: UUID) -> PortForward:
        """
        读取可见端口转发

        :param user (User): 当前用户
        :param forward_id (UUID): 端口转发 ID

        :return PortForward: 端口转发实体
        """

        port_forward = await self._require_visible(user=user, forward_id=forward_id)
        if self._expire_if_needed(port_forward):
            await self._session.commit()
        return port_forward

    async def issue_connection(
        self, *, user: User, token: AuthToken, forward_id: UUID
    ) -> IssuedPortForwardConnection:
        """
        为断线重连签发新的一次性 token

        :param user (User): 当前用户
        :param token (AuthToken): 当前设备 token
        :param forward_id (UUID): 端口转发 ID

        :return IssuedPortForwardConnection: 一次性连接凭证
        """

        device_id = self._require_device_token(token)
        port_forward = await self._require_owned(user=user, forward_id=forward_id)
        await self._ensure_non_terminal(port_forward)
        if port_forward.device_id != device_id:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Port forward belongs to another device.",
                status_code=403,
            )
        await self._require_runtime_authorization(port_forward)
        connection = await self._issue_connection(port_forward)
        await self._audit(
            actor_user_id=user.id,
            action="port_forward.reconnected",
            port_forward=port_forward,
            details={"device_id": str(device_id)},
        )
        await self._session.commit()
        return connection

    async def stop(self, *, user: User, forward_id: UUID) -> PortForward:
        """
        停止端口转发

        :param user (User): 当前用户
        :param forward_id (UUID): 端口转发 ID

        :return PortForward: 已停止转发
        """

        port_forward = await self._require_visible(user=user, forward_id=forward_id)
        if port_forward.status not in TERMINAL_FORWARD_STATUSES:
            port_forward.status = "stopped"
            port_forward.stopped_at = self._now()
            port_forward.stop_reason = (
                "user_stopped" if port_forward.user_id == user.id else "admin_stopped"
            )
            port_forward.lease_expires_at = None
            await self._audit(
                actor_user_id=user.id,
                action="port_forward.stopped",
                port_forward=port_forward,
                details={"reason": port_forward.stop_reason},
            )
            await self._session.commit()
        return port_forward

    async def redeem(
        self,
        *,
        node: Node,
        forward_id: UUID,
        device_id: UUID,
        ssh_key_id: UUID,
        connect_token: str,
    ) -> RedeemedPortForward:
        """
        Node 原子兑换一次性连接 token

        :param node (Node): 当前 Node
        :param forward_id (UUID): 端口转发 ID
        :param device_id (UUID): forced-command 设备 ID
        :param ssh_key_id (UUID): 强制命令 SSH 密钥 ID
        :param connect_token (str): 一次性连接 token

        :return RedeemedPortForward: Node 授权结果
        """

        port_forward = await self._repository.get_for_update(forward_id)
        if port_forward is None or port_forward.node_id != node.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Port forward was not found.", status_code=404
            )
        await self._ensure_non_terminal(port_forward)
        if port_forward.device_id != device_id or (
            port_forward.ssh_key_id is not None and port_forward.ssh_key_id != ssh_key_id
        ):
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="SSH identity does not match port forward.",
                status_code=403,
            )
        active_ssh_key = await self._repository.get_active_ssh_key(
            device_id=device_id, ssh_key_id=ssh_key_id
        )
        if active_ssh_key is None:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="SSH identity does not match an active device key.",
                status_code=403,
            )
        await self._require_rate_limit(
            scope=f"redeem:{node.id}:{device_id}:{forward_id}",
            limit=self._settings.port_forward_redeem_rate_limit_per_minute,
        )
        claims = await self._consume_connection_token(connect_token)
        if (
            claims is None
            or claims.forward_id != port_forward.id
            or claims.device_id != device_id
            or (claims.ssh_key_id is not None and claims.ssh_key_id != ssh_key_id)
        ):
            raise ApiError(
                code="AUTH_INVALID", message="Connection token is invalid.", status_code=401
            )
        if port_forward.ssh_key_id is None:
            port_forward.ssh_key_id = ssh_key_id
        tool_session, policy = await self._require_runtime_authorization(port_forward)
        port_forward.connection_generation += 1
        port_forward.generation_bytes_up = 0
        port_forward.generation_bytes_down = 0
        port_forward.generation_connection_count = 0
        port_forward.status = "active"
        port_forward.last_connected_at = self._now()
        port_forward.lease_expires_at = self._now() + timedelta(seconds=policy.lease_seconds)
        await self._audit(
            actor_user_id=None,
            action="port_forward.connected",
            port_forward=port_forward,
            details={"generation": port_forward.connection_generation},
        )
        await self._session.commit()
        return RedeemedPortForward(
            port_forward=port_forward,
            tool_session=tool_session,
            policy=policy,
        )

    async def renew(
        self,
        *,
        node: Node,
        forward_id: UUID,
        generation: int,
        bytes_up_total: int,
        bytes_down_total: int,
        connection_count_total: int,
    ) -> RedeemedPortForward:
        """
        续租 Node 端口转发授权

        :return RedeemedPortForward: 续租结果
        """

        port_forward = await self._require_node_generation(
            node=node, forward_id=forward_id, generation=generation
        )
        tool_session, policy = await self._require_runtime_authorization(port_forward)
        self._apply_counters(
            port_forward,
            bytes_up_total=bytes_up_total,
            bytes_down_total=bytes_down_total,
            connection_count_total=connection_count_total,
        )
        port_forward.lease_expires_at = self._now() + timedelta(seconds=policy.lease_seconds)
        await self._session.commit()
        return RedeemedPortForward(
            port_forward=port_forward,
            tool_session=tool_session,
            policy=policy,
        )

    async def release(
        self,
        *,
        node: Node,
        forward_id: UUID,
        generation: int,
        bytes_up_total: int,
        bytes_down_total: int,
        connection_count_total: int,
        reason: str,
    ) -> PortForward:
        """
        释放 Node 端口转发连接

        :return PortForward: 释放后的转发实体
        """

        port_forward = await self._require_node_generation(
            node=node, forward_id=forward_id, generation=generation
        )
        self._apply_counters(
            port_forward,
            bytes_up_total=bytes_up_total,
            bytes_down_total=bytes_down_total,
            connection_count_total=connection_count_total,
        )
        if port_forward.status == "active":
            port_forward.status = "disconnected"
            port_forward.lease_expires_at = None
        await self._audit(
            actor_user_id=None,
            action="port_forward.disconnected",
            port_forward=port_forward,
            details={"generation": generation, "reason": reason},
        )
        await self._session.commit()
        return port_forward

    async def _require_node_generation(
        self, *, node: Node, forward_id: UUID, generation: int
    ) -> PortForward:
        port_forward = await self._repository.get_for_update(forward_id)
        if port_forward is None or port_forward.node_id != node.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Port forward was not found.", status_code=404
            )
        if port_forward.connection_generation != generation or port_forward.status != "active":
            raise ApiError(
                code="TUNNEL_EXPIRED",
                message="Port forward connection generation is no longer active.",
                status_code=409,
            )
        return port_forward

    async def _require_runtime_authorization(
        self, port_forward: PortForward
    ) -> tuple[Session, PortForwardPolicy]:
        await self._ensure_non_terminal(port_forward)
        user = await self._repository.get_active_user(port_forward.user_id)
        if user is None:
            await self._revoke(port_forward, "user_revoked")
            raise ApiError(code="USER_REVOKED", message="User is not active.", status_code=403)
        device = await self._repository.get_active_device(
            user_id=port_forward.user_id, device_id=port_forward.device_id
        )
        ssh_key = (
            await self._repository.get_active_ssh_key(
                device_id=port_forward.device_id, ssh_key_id=port_forward.ssh_key_id
            )
            if port_forward.ssh_key_id is not None
            else await self._repository.first_active_ssh_key(port_forward.device_id)
        )
        if device is None or ssh_key is None:
            await self._revoke(port_forward, "device_revoked")
            raise ApiError(code="DEVICE_REVOKED", message="Device is not active.", status_code=403)
        tool_session = await self._repository.get_session(port_forward.session_id)
        if (
            tool_session is None
            or tool_session.status not in RUNNING_SESSION_STATUSES
            or not tool_session.runtime_resource_id
        ):
            await self._revoke(port_forward, "session_not_running")
            raise ApiError(
                code="SESSION_NOT_RUNNING",
                message="Session is not running.",
                status_code=409,
            )
        tool_account = await self._repository.get_active_tool_account(tool_session.tool_account_id)
        if tool_account is None:
            await self._revoke(port_forward, "tool_account_revoked")
            raise ApiError(
                code="TOOL_ACCOUNT_REVOKED",
                message="Tool account is not active.",
                status_code=403,
            )
        node = await self._require_node(port_forward.node_id)
        policy = self._policy(node)
        self._require_capability(node, tool_session)
        self._validate_remote_port(policy, port_forward.remote_port)
        return tool_session, policy

    async def _require_running_session(self, user_id: UUID, session_id: UUID) -> Session:
        tool_session = await self._repository.get_session(session_id)
        if tool_session is None or tool_session.user_id != user_id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        if (
            tool_session.status not in RUNNING_SESSION_STATUSES
            or not tool_session.runtime_resource_id
        ):
            raise ApiError(
                code="SESSION_NOT_RUNNING",
                message="Session runtime is not ready.",
                status_code=409,
            )
        return tool_session

    async def _require_node(self, node_id: UUID) -> Node:
        node = await self._repository.get_node(node_id)
        if node is None:
            raise ApiError(code="COMMON_NOT_FOUND", message="Node was not found.", status_code=404)
        if node.status not in ACTIVE_NODE_STATUSES:
            raise ApiError(code="NODE_UNHEALTHY", message="Node is not available.", status_code=409)
        return node

    async def cleanup(
        self, *, limit: int = 100, after_id: UUID | None = None
    ) -> PortForwardCleanupResult:
        """
        对账非终态端口转发并收敛生命周期

        :param limit (int): 单次最大处理数

        :param after_id (UUID): 上一批最后处理的转发 ID

        :return PortForwardCleanupResult: 状态变更数和下一批游标
        """

        changed = 0
        forward_ids = await self._repository.list_non_terminal_ids(limit, after_id=after_id)
        for forward_id in forward_ids:
            port_forward = await self._repository.get_for_update(forward_id)
            if port_forward is None or port_forward.status not in NON_TERMINAL_FORWARD_STATUSES:
                continue
            try:
                await self._ensure_non_terminal(port_forward)
            except ApiError as error:
                if error.code == "AUTH_EXPIRED":
                    changed += 1
                continue
            try:
                await self._require_runtime_authorization(port_forward)
            except ApiError as error:
                if port_forward.status == "revoked":
                    changed += 1
                    continue
                if error.code in {
                    "COMMON_NOT_FOUND",
                    "NODE_UNHEALTHY",
                    "PORT_FORWARD_DISABLED",
                    "PORT_FORWARD_UNSUPPORTED",
                    "PORT_NOT_ALLOWED",
                    "PROTOCOL_UNSUPPORTED",
                }:
                    await self._revoke(port_forward, "authorization_changed")
                    changed += 1
                continue
            if (
                port_forward.status == "active"
                and port_forward.lease_expires_at is not None
                and self._aware(port_forward.lease_expires_at) <= self._now()
            ):
                port_forward.status = "disconnected"
                port_forward.lease_expires_at = None
                await self._audit(
                    actor_user_id=None,
                    action="port_forward.disconnected",
                    port_forward=port_forward,
                    details={"reason": "lease_expired"},
                )
                await self._session.commit()
                changed += 1
        next_cursor = forward_ids[-1] if len(forward_ids) == limit else None
        return PortForwardCleanupResult(changed=changed, next_cursor=next_cursor)

    def _require_capability(self, node: Node, tool_session: Session) -> None:
        capability = node.runtime_capabilities.get("session_port_forwarding")
        if not isinstance(capability, dict) or capability.get("supported") is not True:
            raise ApiError(
                code="PORT_FORWARD_UNSUPPORTED",
                message="Node does not support session port forwarding.",
                status_code=409,
            )
        backends = capability.get("backends")
        if not isinstance(backends, list) or tool_session.runtime_backend not in backends:
            raise ApiError(
                code="PORT_FORWARD_UNSUPPORTED",
                message="Session runtime does not support port forwarding.",
                status_code=409,
            )
        protocol_versions = capability.get("protocol_versions")
        max_streams = capability.get("max_streams")
        if (
            not isinstance(protocol_versions, list)
            or 1 not in protocol_versions
            or not isinstance(max_streams, int)
            or isinstance(max_streams, bool)
            or not 1 <= max_streams <= 1024
        ):
            raise ApiError(
                code="PROTOCOL_UNSUPPORTED",
                message="Node port forwarding protocol is not compatible.",
                status_code=409,
            )

    async def _validate_quotas(
        self,
        *,
        policy: PortForwardPolicy,
        user_id: UUID,
        device_id: UUID,
        session_id: UUID,
    ) -> None:
        checks = (
            (await self._repository.count_active(user_id=user_id), policy.max_per_user, "user"),
            (
                await self._repository.count_active(device_id=device_id),
                policy.max_per_device,
                "device",
            ),
            (
                await self._repository.count_active(session_id=session_id),
                policy.max_per_session,
                "session",
            ),
        )
        for current, maximum, scope in checks:
            if current >= maximum:
                raise ApiError(
                    code="POLICY_LIMIT",
                    message="Port forward quota has been reached.",
                    status_code=409,
                    details={"scope": scope, "maximum": maximum},
                )

    def _policy(self, node: Node) -> PortForwardPolicy:
        values = node.runtime_policy.get("port_forwarding")
        overrides = values if isinstance(values, dict) else {}
        denied_values = overrides.get("denied_ports", [])
        denied_ports = (
            frozenset(
                value
                for value in denied_values
                if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
            )
            if isinstance(denied_values, list)
            else frozenset()
        )
        capability = node.runtime_capabilities.get("session_port_forwarding")
        raw_capability_max_streams: object = (
            capability.get("max_streams") if isinstance(capability, dict) else None
        )
        capability_max_streams = (
            raw_capability_max_streams
            if isinstance(raw_capability_max_streams, int)
            and not isinstance(raw_capability_max_streams, bool)
            and 1 <= raw_capability_max_streams <= 1024
            else self._settings.port_forward_max_streams
        )
        min_port = self._int_policy(
            overrides, "min_port", self._settings.port_forward_min_port, maximum=65535
        )
        max_port = self._int_policy(
            overrides, "max_port", self._settings.port_forward_max_port, maximum=65535
        )
        max_ttl_seconds = self._int_policy(
            overrides,
            "max_ttl_seconds",
            self._settings.port_forward_max_ttl_seconds,
            minimum=60,
        )
        default_ttl_seconds = min(
            self._int_policy(
                overrides,
                "default_ttl_seconds",
                self._settings.port_forward_default_ttl_seconds,
                minimum=60,
            ),
            max_ttl_seconds,
        )
        return PortForwardPolicy(
            enabled=self._bool_policy(overrides, "enabled", self._settings.port_forwarding_enabled),
            min_port=min_port,
            max_port=max_port,
            denied_ports=denied_ports,
            max_per_user=self._int_policy(
                overrides, "max_per_user", self._settings.port_forward_max_per_user
            ),
            max_per_device=self._int_policy(
                overrides, "max_per_device", self._settings.port_forward_max_per_device
            ),
            max_per_session=self._int_policy(
                overrides, "max_per_session", self._settings.port_forward_max_per_session
            ),
            max_streams=self._int_policy(
                overrides,
                "max_streams",
                min(self._settings.port_forward_max_streams, capability_max_streams),
                maximum=capability_max_streams,
            ),
            default_ttl_seconds=default_ttl_seconds,
            max_ttl_seconds=max_ttl_seconds,
            lease_seconds=self._int_policy(
                overrides, "lease_seconds", self._settings.port_forward_lease_seconds
            ),
            control_plane_grace_seconds=self._int_policy(
                overrides,
                "control_plane_grace_seconds",
                self._settings.port_forward_control_plane_grace_seconds,
                minimum=0,
            ),
            bytes_per_second=self._int_policy(
                overrides,
                "bytes_per_second",
                self._settings.port_forward_bytes_per_second,
                minimum=0,
            ),
        )

    def _validate_remote_port(self, policy: PortForwardPolicy, remote_port: int) -> None:
        if not policy.enabled:
            raise ApiError(
                code="PORT_FORWARD_DISABLED",
                message="Session port forwarding is disabled.",
                status_code=403,
            )
        if (
            policy.min_port > policy.max_port
            or not policy.min_port <= remote_port <= policy.max_port
            or remote_port in policy.denied_ports
        ):
            raise ApiError(
                code="PORT_NOT_ALLOWED",
                message="Remote port is not allowed by policy.",
                status_code=403,
            )

    async def _issue_connection(self, port_forward: PortForward) -> IssuedPortForwardConnection:
        raw_token = create_opaque_token("pfc")
        token_hash = hash_token(self._settings.secret_key, raw_token)
        ttl = self._settings.port_forward_connection_token_ttl_seconds
        try:
            await self._token_store.issue(
                token_hash=token_hash,
                claims=PortForwardTokenClaims(
                    forward_id=port_forward.id,
                    device_id=port_forward.device_id,
                    ssh_key_id=port_forward.ssh_key_id,
                ),
                ttl=ttl,
            )
        except Exception as exc:
            raise ApiError(
                code="CONTROL_PLANE_UNAVAILABLE",
                message="Unable to issue a port forward connection token.",
                status_code=503,
            ) from exc
        return IssuedPortForwardConnection(
            token=raw_token,
            expires_at=self._now() + timedelta(seconds=ttl),
        )

    async def _consume_connection_token(self, raw_token: str) -> PortForwardTokenClaims | None:
        try:
            return await self._token_store.consume(
                token_hash=hash_token(self._settings.secret_key, raw_token)
            )
        except Exception as exc:
            raise ApiError(
                code="CONTROL_PLANE_UNAVAILABLE",
                message="Unable to validate the port forward connection token.",
                status_code=503,
            ) from exc

    async def _require_rate_limit(self, *, scope: str, limit: int) -> None:
        try:
            allowed = await self._token_store.allow(
                scope=scope,
                limit=limit,
                window_seconds=60,
            )
        except Exception as exc:
            raise ApiError(
                code="CONTROL_PLANE_UNAVAILABLE",
                message="Unable to enforce port forward rate limits.",
                status_code=503,
            ) from exc
        if not allowed:
            raise ApiError(
                code="RATE_LIMITED",
                message="Port forward request rate limit exceeded.",
                status_code=429,
            )

    async def _require_owned(self, *, user: User, forward_id: UUID) -> PortForward:
        port_forward = await self._repository.get(forward_id)
        if port_forward is None or port_forward.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Port forward was not found.", status_code=404
            )
        return port_forward

    async def _require_visible(self, *, user: User, forward_id: UUID) -> PortForward:
        port_forward = await self._repository.get(forward_id)
        if port_forward is None or (port_forward.user_id != user.id and user.role != "admin"):
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Port forward was not found.", status_code=404
            )
        return port_forward

    async def _ensure_non_terminal(self, port_forward: PortForward) -> None:
        if self._expire_if_needed(port_forward):
            await self._audit(
                actor_user_id=None,
                action="port_forward.expired",
                port_forward=port_forward,
                details={"reason": "ttl_expired"},
            )
            await self._session.commit()
            raise ApiError(
                code="AUTH_EXPIRED", message="Port forward has expired.", status_code=410
            )
        if port_forward.status not in NON_TERMINAL_FORWARD_STATUSES:
            raise ApiError(
                code="TUNNEL_EXPIRED", message="Port forward is no longer active.", status_code=409
            )

    def _expire_if_needed(self, port_forward: PortForward) -> bool:
        if (
            port_forward.status in NON_TERMINAL_FORWARD_STATUSES
            and self._aware(port_forward.expires_at) <= self._now()
        ):
            port_forward.status = "expired"
            port_forward.stopped_at = self._now()
            port_forward.stop_reason = "ttl_expired"
            port_forward.lease_expires_at = None
            return True
        return False

    async def _revoke(self, port_forward: PortForward, reason: str) -> None:
        port_forward.status = "revoked"
        port_forward.stopped_at = self._now()
        port_forward.stop_reason = reason
        port_forward.lease_expires_at = None
        await self._audit(
            actor_user_id=None,
            action="port_forward.revoked",
            port_forward=port_forward,
            details={"reason": reason},
        )
        await self._session.commit()

    def _apply_counters(
        self,
        port_forward: PortForward,
        *,
        bytes_up_total: int,
        bytes_down_total: int,
        connection_count_total: int,
    ) -> None:
        next_bytes_up = max(port_forward.generation_bytes_up, bytes_up_total)
        next_bytes_down = max(port_forward.generation_bytes_down, bytes_down_total)
        next_connection_count = max(
            port_forward.generation_connection_count, connection_count_total
        )
        port_forward.bytes_up += next_bytes_up - port_forward.generation_bytes_up
        port_forward.bytes_down += next_bytes_down - port_forward.generation_bytes_down
        port_forward.connection_count += (
            next_connection_count - port_forward.generation_connection_count
        )
        port_forward.generation_bytes_up = next_bytes_up
        port_forward.generation_bytes_down = next_bytes_down
        port_forward.generation_connection_count = next_connection_count

    async def _audit(
        self,
        *,
        actor_user_id: UUID | None,
        action: str,
        port_forward: PortForward,
        details: dict[str, object],
    ) -> None:
        await self._identity_repository.add_audit_log(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type="port_forward",
                target_id=str(port_forward.id),
                details=details,
            )
        )

    def _require_device_token(self, token: AuthToken) -> UUID:
        if token.user_device_id is None:
            raise ApiError(
                code="DEVICE_REQUIRED",
                message="A registered device token is required.",
                status_code=403,
            )
        return token.user_device_id

    def _int_policy(
        self,
        values: dict[str, object],
        key: str,
        default: int,
        *,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        value = values.get(key)
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and value >= minimum
            and (maximum is None or value <= maximum)
            else default
        )

    def _bool_policy(self, values: dict[str, object], key: str, default: bool) -> bool:
        value = values.get(key)
        return value if isinstance(value, bool) else default

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _aware(self, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
