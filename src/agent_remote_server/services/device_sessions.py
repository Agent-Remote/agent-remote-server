from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.device_control_limits import (
    MAX_ACTIVE_DEVICE_SESSION_GENERATION,
    MAX_DEVICE_SESSION_GENERATION,
)
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    DeviceSession,
    DeviceSessionApproval,
    NodeTask,
    Session,
    User,
)
from agent_remote_server.repositories.device_sessions import DeviceSessionRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.schemas.device_sessions import DeviceApprovalItem

ACTIVE_TOOL_STATUSES = {"running", "active", "detached"}
TERMINAL_DEVICE_STATUSES = {"stopped", "denied", "expired", "failed"}


class DeviceSessionService:
    """
    本地设备控制会话授权与生命周期服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        初始化本地设备控制会话授权与生命周期服务

        :param session (AsyncSession): 异步数据库会话
        :param settings (Settings): 应用配置
        """

        self._session = session
        self._settings = settings
        self._repository = DeviceSessionRepository(session)
        self._identity_repository = IdentityRepository(session)

    async def create(
        self, *, user: User, token: AuthToken, device_id: UUID, tool_session_id: UUID
    ) -> DeviceSession:
        """
        创建与用户、设备及远端 session 严格绑定的设备控制会话

        :param user (User): 当前用户
        :param token (AuthToken): 当前用户认证令牌
        :param device_id (UUID): 被控制设备 ID
        :param tool_session_id (UUID): 远端工具 session ID

        :return DeviceSession: 新建设备控制会话
        """

        if not self._settings.device_control_enabled:
            raise ApiError(
                code="DEVICE_CONTROL_DISABLED",
                message="Device control is disabled by deployment policy.",
                status_code=503,
            )
        if token.token_type != "user":
            raise ApiError(
                code="COMMON_FORBIDDEN", message="A user token is required.", status_code=403
            )
        device = await self._repository.get_device(device_id)
        if device is None or device.user_id != user.id or device.status != "active":
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Device was not found.", status_code=404
            )
        if device.platform != "macos":
            raise ApiError(
                code="DEVICE_CONTROL_PLATFORM_UNSUPPORTED",
                message="Device control requires macOS.",
                status_code=409,
            )
        tool_session = await self._repository.get_tool_session(tool_session_id)
        if tool_session is None or tool_session.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Tool session was not found.", status_code=404
            )
        if tool_session.status not in ACTIVE_TOOL_STATUSES:
            raise ApiError(
                code="DEVICE_CONTROL_TOOL_SESSION_INACTIVE",
                message="Tool session is not active.",
                status_code=409,
            )
        if tool_session.device_control_protocol_version != 1:
            raise ApiError(
                code="DEVICE_CONTROL_TOOL_SESSION_NOT_CONFIGURED",
                message="Tool session was not started with managed device control.",
                status_code=409,
            )
        node = await self._repository.get_node(tool_session.node_id)
        if node is None or not self._supports_device_control(
            node.runtime_capabilities, tool_session.runtime_backend
        ):
            raise ApiError(
                code="DEVICE_CONTROL_NODE_UNAVAILABLE",
                message="The assigned node has not reported a compatible device proxy.",
                status_code=409,
            )

        now = self._now()
        device_session = DeviceSession(
            user_id=user.id,
            device_id=device.id,
            tool_session_id=tool_session.id,
            node_id=tool_session.node_id,
            platform="macos",
            status="pending_device",
            generation=1,
            expires_at=now + timedelta(seconds=self._settings.device_session_max_ttl_seconds),
        )
        try:
            await self._repository.add(device_session)
            await self._enqueue_activation(device_session, tool_session)
            await self._audit(user.id, "device_session.create", device_session, {})
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ApiError(
                code="DEVICE_CONTROL_SESSION_EXISTS",
                message="This tool session already has a device session.",
                status_code=409,
            ) from exc
        return device_session

    async def list_for_user(self, *, user: User) -> list[DeviceSession]:
        """
        列出当前用户的设备控制会话

        :param user (User): 当前用户

        :return list[DeviceSession]: 设备控制会话列表
        """

        return list(await self._repository.list_for_user(user.id))

    async def list_for_admin(self, *, user: User) -> list[DeviceSession]:
        """
        列出管理员可见的全部设备控制会话

        :param user (User): 当前管理员用户

        :return list[DeviceSession]: 全部设备控制会话列表
        """

        if user.role != "admin":
            raise ApiError(code="COMMON_FORBIDDEN", message="Admin role required.", status_code=403)
        return list(await self._repository.list_all())

    async def list_for_device(self, *, token: AuthToken) -> list[DeviceSession]:
        """
        列出当前认证设备可处理的非终态控制会话

        :param token (AuthToken): 当前设备认证令牌

        :return list[DeviceSession]: 严格绑定当前设备的控制会话列表
        """

        if token.token_type != "device" or token.user_device_id is None:
            raise ApiError(
                code="COMMON_FORBIDDEN", message="A device token is required.", status_code=403
            )
        items = list(await self._repository.list_for_device(token.user_device_id))
        expired = False
        for item in items:
            previous_status = item.status
            self._expire_if_needed(item)
            expired = expired or item.status != previous_status
        if expired:
            await self._session.commit()
        return [item for item in items if item.status not in TERMINAL_DEVICE_STATUSES]

    async def get_for_user(self, *, user: User, device_session_id: UUID) -> DeviceSession:
        """
        读取当前用户拥有的设备控制会话

        :param user (User): 当前用户
        :param device_session_id (UUID): 设备控制会话 ID

        :return DeviceSession: 设备控制会话实体
        """

        device_session = await self._require(device_session_id)
        if device_session.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Device session was not found.", status_code=404
            )
        self._expire_if_needed(device_session)
        if device_session.status == "expired":
            await self._session.commit()
        return device_session

    async def mark_device_connected(
        self, *, token: AuthToken, device_session_id: UUID, generation: int
    ) -> DeviceSession:
        """
        由绑定设备确认通道已连接并等待本机用户审批

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 当前连接代次

        :return DeviceSession: 更新后的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        self._require_generation(device_session, generation)
        await self._require_not_expired(device_session)
        if device_session.status != "pending_device":
            raise self._state_conflict()
        device_session.status = "pending_user_approval"
        await self._audit(
            device_session.user_id, "device_session.device_connected", device_session, {}
        )
        await self._session.commit()
        return device_session

    async def approve(
        self,
        *,
        token: AuthToken,
        device_session_id: UUID,
        generation: int,
        approvals: list[DeviceApprovalItem],
    ) -> DeviceSession:
        """
        只接受绑定设备提交的本机应用审批摘要

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 当前连接代次
        :param approvals (list[DeviceApprovalItem]): 本机应用审批摘要列表

        :return DeviceSession: 审批后的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        self._require_generation(device_session, generation)
        await self._require_not_expired(device_session)
        if device_session.status != "pending_user_approval":
            raise self._state_conflict()
        if len({item.application_digest for item in approvals}) != len(approvals):
            raise ApiError(
                code="DEVICE_CONTROL_DUPLICATE_APPLICATION",
                message="Application approvals must be unique.",
                status_code=422,
            )
        records = [
            DeviceSessionApproval(
                device_session_id=device_session.id,
                application_digest=item.application_digest,
                control_level=item.control_level,
                approval_result=item.approval_result,
                clipboard_allowed=item.clipboard_allowed,
                audit_correlation_id=uuid4(),
            )
            for item in approvals
        ]
        await self._repository.replace_approvals(device_session.id, records)
        allowed_count = sum(item.approval_result == "allowed" for item in approvals)
        if allowed_count == 0:
            device_session.status = "denied"
            device_session.stopped_at = self._now()
            device_session.stop_reason = "user_denied"
            await self._enqueue_deactivation(device_session)
        else:
            device_session.status = "active"
            device_session.lease_until = min(
                self._now() + timedelta(seconds=self._settings.device_session_lease_seconds),
                self._aware(device_session.expires_at),
            )
            await self._enqueue_context_update(device_session)
        await self._audit(
            device_session.user_id,
            "device_session.approve",
            device_session,
            {"allowed_count": allowed_count, "denied_count": len(approvals) - allowed_count},
        )
        await self._session.commit()
        return device_session

    async def acquire_lock(
        self, *, token: AuthToken, device_session_id: UUID, generation: int
    ) -> DeviceSession:
        """
        在首次成功动作后获取机器级单 session 锁

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 当前连接代次

        :return DeviceSession: 已持有机器锁的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        await self._require_active_generation(device_session, generation)
        if device_session.lock_acquired_at is not None:
            return device_session
        holder = await self._repository.find_machine_lock(
            device_session.device_id, excluding_session_id=device_session.id
        )
        if holder is not None:
            raise ApiError(
                code="DEVICE_CONTROL_MACHINE_LOCKED",
                message="The device is controlled by another session.",
                status_code=409,
                details={"holder_tool_session_id": str(holder.tool_session_id)},
            )
        device_session.lock_acquired_at = self._now()
        await self._audit(device_session.user_id, "device_session.lock", device_session, {})
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ApiError(
                code="DEVICE_CONTROL_MACHINE_LOCKED",
                message="The device is controlled by another session.",
                status_code=409,
            ) from exc
        return device_session

    async def renew(
        self, *, token: AuthToken, device_session_id: UUID, generation: int
    ) -> DeviceSession:
        """
        由绑定设备续订当前代次的短租约

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 当前连接代次

        :return DeviceSession: 已续租的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        await self._require_active_generation(device_session, generation)
        device_session.lease_until = min(
            self._now() + timedelta(seconds=self._settings.device_session_lease_seconds),
            self._aware(device_session.expires_at),
        )
        await self._enqueue_context_update(device_session)
        await self._session.commit()
        return device_session

    async def reconnect(
        self, *, token: AuthToken, device_session_id: UUID, generation: int
    ) -> DeviceSession:
        """
        断线后创建新代次且不保留旧租约

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 断线前连接代次

        :return DeviceSession: 进入新代次的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        self._require_generation(device_session, generation)
        await self._require_not_expired(device_session)
        if device_session.status not in {"pending_device", "pending_user_approval", "active"}:
            raise self._state_conflict()
        self._increment_generation(device_session, terminal=False)
        device_session.status = "pending_device"
        device_session.lease_until = None
        tool_session = await self._require_tool_session(device_session.tool_session_id)
        await self._enqueue_activation(device_session, tool_session)
        await self._audit(
            device_session.user_id,
            "device_session.reconnect",
            device_session,
            {"generation": device_session.generation},
        )
        await self._session.commit()
        return device_session

    async def abort_action(
        self,
        *,
        token: AuthToken,
        device_session_id: UUID,
        generation: int,
        reason: str,
    ) -> DeviceSession:
        """
        中止当前动作并保留机器锁和远端 session 绑定

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 被中止动作所在连接代次
        :param reason (str): 不含敏感内容的中止原因

        :return DeviceSession: 等待新代次连接的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        self._require_generation(device_session, generation)
        await self._require_not_expired(device_session)
        if device_session.status != "active":
            raise self._state_conflict()
        self._increment_generation(device_session, terminal=False)
        device_session.status = "pending_device"
        device_session.lease_until = None
        tool_session = await self._require_tool_session(device_session.tool_session_id)
        await self._enqueue_activation(device_session, tool_session)
        await self._audit(
            device_session.user_id,
            "device_session.abort_action",
            device_session,
            {"reason": reason, "generation": device_session.generation},
        )
        await self._session.commit()
        return device_session

    async def stop_by_user(
        self, *, user: User, device_session_id: UUID, reason: str
    ) -> DeviceSession:
        """
        由当前用户结束设备控制并立即撤销租约与锁

        :param user (User): 当前用户
        :param device_session_id (UUID): 设备控制会话 ID
        :param reason (str): 不含敏感内容的停止原因

        :return DeviceSession: 已停止的设备控制会话
        """

        device_session = await self.get_for_user(user=user, device_session_id=device_session_id)
        return await self._stop(device_session, reason)

    async def stop_by_device(
        self, *, token: AuthToken, device_session_id: UUID, reason: str
    ) -> DeviceSession:
        """
        由绑定设备全局停止并立即撤销租约与锁

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param reason (str): 不含敏感内容的停止原因

        :return DeviceSession: 已停止的设备控制会话
        """

        device_session = await self._require_device(token, device_session_id, for_update=True)
        return await self._stop(device_session, reason)

    async def stop_by_admin(
        self, *, user: User, device_session_id: UUID, reason: str
    ) -> DeviceSession:
        """
        由管理员强制结束设备控制并立即撤销租约与锁

        :param user (User): 当前管理员用户
        :param device_session_id (UUID): 设备控制会话 ID
        :param reason (str): 不含敏感内容的停止原因

        :return DeviceSession: 已停止的设备控制会话
        """

        if user.role != "admin":
            raise ApiError(code="COMMON_FORBIDDEN", message="Admin role required.", status_code=403)
        device_session = await self._require(device_session_id, for_update=True)
        return await self._stop(
            device_session,
            reason,
            actor_user_id=user.id,
            audit_action="device_session.admin_stop",
        )

    async def _stop(
        self,
        device_session: DeviceSession,
        reason: str,
        *,
        actor_user_id: UUID | None = None,
        audit_action: str = "device_session.stop",
    ) -> DeviceSession:
        if device_session.status in TERMINAL_DEVICE_STATUSES:
            return device_session
        self._increment_generation(device_session, terminal=True)
        device_session.status = "stopped"
        device_session.lease_until = None
        device_session.lock_acquired_at = None
        device_session.stopped_at = self._now()
        device_session.stop_reason = reason
        await self._enqueue_deactivation(device_session)
        await self._audit(
            actor_user_id or device_session.user_id,
            audit_action,
            device_session,
            {"reason": reason},
        )
        await self._session.commit()
        return device_session

    async def _require_tool_session(self, tool_session_id: UUID) -> Session:
        tool_session = await self._repository.get_tool_session(tool_session_id)
        if tool_session is None:
            raise ApiError(
                code="DEVICE_CONTROL_TOOL_SESSION_INACTIVE",
                message="Tool session is not active.",
                status_code=409,
            )
        return tool_session

    async def _enqueue_activation(
        self,
        device_session: DeviceSession,
        tool_session: Session,
    ) -> None:
        await self._repository.add_task(
            NodeTask(
                task_id=(
                    f"activate_device_control:{device_session.id}:{device_session.generation}"
                ),
                node_id=device_session.node_id,
                task_type="activate_device_control",
                status="pending",
                payload={
                    "protocol_version": 1,
                    "user_id": str(device_session.user_id),
                    "device_id": str(device_session.device_id),
                    "tool_session_id": str(device_session.tool_session_id),
                    "device_session_id": str(device_session.id),
                    "node_id": str(device_session.node_id),
                    "platform": "macos",
                    "generation": device_session.generation,
                    "expires_at": self._aware(device_session.expires_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "runtime_backend": tool_session.runtime_backend,
                    "runtime_resource_id": tool_session.runtime_resource_id,
                },
            )
        )

    async def _enqueue_deactivation(self, device_session: DeviceSession) -> None:
        await self._repository.add_task(
            NodeTask(
                task_id=(
                    f"deactivate_device_control:{device_session.id}:{device_session.generation}"
                ),
                node_id=device_session.node_id,
                task_type="deactivate_device_control",
                status="pending",
                payload={
                    "device_session_id": str(device_session.id),
                    "tool_session_id": str(device_session.tool_session_id),
                    "generation": device_session.generation,
                },
            )
        )

    async def _enqueue_context_update(self, device_session: DeviceSession) -> None:
        if device_session.lease_until is None:
            raise RuntimeError("active device session omitted its lease")
        lease_until = self._aware(device_session.lease_until)
        lease_identity = int(lease_until.timestamp() * 1_000_000)
        await self._repository.add_task(
            NodeTask(
                task_id=(
                    f"update_device_control_context:{device_session.id}:"
                    f"{device_session.generation}:{lease_identity}"
                ),
                node_id=device_session.node_id,
                task_type="update_device_control_context",
                status="pending",
                payload={
                    "protocol_version": 1,
                    "user_id": str(device_session.user_id),
                    "device_id": str(device_session.device_id),
                    "tool_session_id": str(device_session.tool_session_id),
                    "device_session_id": str(device_session.id),
                    "node_id": str(device_session.node_id),
                    "platform": "macos",
                    "generation": device_session.generation,
                    "lease_until": lease_until.isoformat().replace("+00:00", "Z"),
                },
            )
        )

    async def _require(self, device_session_id: UUID, *, for_update: bool = False) -> DeviceSession:
        device_session = await self._repository.get(device_session_id, for_update=for_update)
        if device_session is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Device session was not found.", status_code=404
            )
        return device_session

    async def _require_device(
        self, token: AuthToken, device_session_id: UUID, *, for_update: bool = False
    ) -> DeviceSession:
        device_session = await self._require(device_session_id, for_update=for_update)
        if token.token_type != "device" or token.user_device_id != device_session.device_id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Device session was not found.", status_code=404
            )
        return device_session

    def _require_generation(self, device_session: DeviceSession, generation: int) -> None:
        if generation != device_session.generation:
            raise ApiError(
                code="DEVICE_CONTROL_GENERATION_MISMATCH",
                message="Device session generation does not match.",
                status_code=409,
            )

    def _increment_generation(self, device_session: DeviceSession, *, terminal: bool) -> None:
        """
        在共享持久化边界内递增设备会话代次

        :param device_session (DeviceSession): 待递增代次的设备控制会话
        :param terminal (bool): 递增后是否立即进入终态

        :raises ApiError: 当前代次已没有可用的安全递增空间
        """

        maximum = (
            MAX_DEVICE_SESSION_GENERATION if terminal else MAX_ACTIVE_DEVICE_SESSION_GENERATION
        )
        if device_session.generation >= maximum:
            raise ApiError(
                code="DEVICE_CONTROL_GENERATION_EXHAUSTED",
                message="Device session generation is exhausted.",
                status_code=409,
            )
        device_session.generation += 1

    async def _require_active_generation(
        self, device_session: DeviceSession, generation: int
    ) -> None:
        self._require_generation(device_session, generation)
        await self._require_not_expired(device_session)
        if device_session.status != "active":
            raise self._state_conflict()
        if (
            device_session.lease_until is None
            or self._aware(device_session.lease_until) <= self._now()
        ):
            device_session.status = "expired"
            device_session.lease_until = None
            device_session.lock_acquired_at = None
            device_session.stopped_at = self._now()
            device_session.stop_reason = "lease_expired"
            await self._session.commit()
            raise ApiError(
                code="DEVICE_CONTROL_LEASE_EXPIRED",
                message="Device session lease has expired.",
                status_code=409,
            )

    async def _require_not_expired(self, device_session: DeviceSession) -> None:
        self._expire_if_needed(device_session)
        if device_session.status == "expired":
            await self._session.commit()
            raise ApiError(
                code="DEVICE_CONTROL_SESSION_EXPIRED",
                message="Device session has expired.",
                status_code=409,
            )

    def _expire_if_needed(self, device_session: DeviceSession) -> None:
        if (
            device_session.status not in TERMINAL_DEVICE_STATUSES
            and self._aware(device_session.expires_at) <= self._now()
        ):
            device_session.status = "expired"
            device_session.lease_until = None
            device_session.lock_acquired_at = None
            device_session.stopped_at = self._now()
            device_session.stop_reason = "session_expired"

    async def _audit(
        self,
        actor_user_id: UUID,
        action: str,
        device_session: DeviceSession,
        details: dict[str, object],
    ) -> None:
        await self._identity_repository.add_audit_log(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type="device_session",
                target_id=str(device_session.id),
                details=details,
            )
        )

    def _state_conflict(self) -> ApiError:
        return ApiError(
            code="DEVICE_CONTROL_STATE_CONFLICT",
            message="Device session state does not permit this operation.",
            status_code=409,
        )

    def _supports_device_control(
        self, runtime_capabilities: dict[str, object], runtime_backend: str
    ) -> bool:
        capability = runtime_capabilities.get("device_control")
        if not isinstance(capability, dict) or capability.get("supported") is not True:
            return False
        protocols = capability.get("protocol_versions")
        platforms = capability.get("platforms")
        backends = capability.get("backends")
        return (
            isinstance(protocols, list)
            and 1 in protocols
            and isinstance(platforms, list)
            and "macos" in platforms
            and isinstance(backends, list)
            and runtime_backend in backends
        )

    def _aware(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def _now(self) -> datetime:
        return datetime.now(UTC)
