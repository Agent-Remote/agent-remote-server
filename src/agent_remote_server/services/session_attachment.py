"""
实现精确 Claude 会话附件租约、清理回执和生命周期失效。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    Node,
    Session,
    SessionAttachmentCleanupReceipt,
    SessionAttachmentLease,
    SshKey,
    ToolAccount,
    UserDevice,
)
from agent_remote_server.schemas.session_attachment import (
    SessionAttachmentAdmissionData,
    SessionAttachmentAdmissionRequest,
    SessionAttachmentCleanupRequest,
)

LEASE_SECONDS = 300
MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 32
LIVE_LEASE_STATES = ("issued", "cleanup_pending")
TERMINAL_LEASE_STATES = ("cleaned", "expired", "revoked", "rejected")


class SessionAttachmentService:
    """
    在 Node 身份边界执行附件传输的严格授权。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定当前请求事务。

        :param session (AsyncSession): 当前请求数据库事务
        """

        self.session = session

    async def admit(
        self, node: Node, payload: SessionAttachmentAdmissionRequest
    ) -> SessionAttachmentAdmissionData:
        """
        原子重验身份、生命周期和能力后签发单用途租约。

        :param node (Node): 已认证节点
        :param payload (SessionAttachmentAdmissionRequest): Node 提交的完整身份
        :return SessionAttachmentAdmissionData: 不含账户路径的租约
        :raises ApiError: 身份、能力、生命周期或并发租约不满足
        """

        self._require_uuid(payload.node_id, "node_id")
        self._require_uuid(payload.session_id, "session_id")
        self._require_uuid(payload.device_id, "device_id")
        self._require_uuid(payload.ssh_key_id, "ssh_key_id")
        self._require_uuid(payload.lease_id, "lease_id")
        if node.id != payload.node_id:
            raise attachment_error("ATTACHMENT_NODE_MISMATCH", 403)
        self._require_capability(node)
        if node.status not in {"healthy", "degraded"}:
            raise attachment_error("NODE_UNAVAILABLE", 409)
        runtime = await self.session.scalar(
            select(Session)
            .where(Session.id == payload.session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if runtime is None:
            raise attachment_error("COMMON_NOT_FOUND", 404)
        if runtime.node_id != node.id:
            raise attachment_error("ATTACHMENT_NODE_MISMATCH", 403)
        if runtime.tool_type != "claude":
            raise attachment_error("SESSION_TOOL_UNSUPPORTED")
        if runtime.status not in {"running", "active"} or not runtime.tmux_session_name:
            raise attachment_error("SESSION_NOT_ATTACHABLE")
        if runtime.runtime_backend not in {"native", "docker_sandbox"}:
            raise attachment_error("SESSION_RUNTIME_UNSUPPORTED")
        account = await self.session.get(ToolAccount, runtime.tool_account_id)
        if (
            account is None
            or account.user_id != runtime.user_id
            or account.tool_type != "claude"
            or account.status != "active"
        ):
            raise attachment_error("SESSION_ACCOUNT_UNAVAILABLE")
        device = await self.session.scalar(
            select(UserDevice).where(
                UserDevice.id == payload.device_id,
                UserDevice.user_id == runtime.user_id,
                UserDevice.status == "active",
            )
        )
        if device is None:
            raise attachment_error("DEVICE_REVOKED", 403)
        key = await self.session.scalar(
            select(SshKey).where(
                SshKey.id == payload.ssh_key_id,
                SshKey.user_device_id == device.id,
                SshKey.status == "active",
            )
        )
        if key is None:
            raise attachment_error("SSH_KEY_REVOKED", 403)
        existing = await self.session.scalar(
            select(SessionAttachmentLease)
            .where(SessionAttachmentLease.id == payload.lease_id)
            .with_for_update()
        )
        if existing is not None:
            raise attachment_error("ATTACHMENT_LEASE_REPLAY", 409)
        leases = list(
            await self.session.scalars(
                select(SessionAttachmentLease)
                .where(SessionAttachmentLease.session_id == runtime.id)
                .where(SessionAttachmentLease.status.in_(LIVE_LEASE_STATES))
                .with_for_update()
            )
        )
        now = datetime.now(UTC)
        for lease in leases:
            expires_at = _aware(lease.expires_at)
            if expires_at <= now:
                lease.status = "expired"
                lease.invalidated_at = now
                lease.invalidation_reason = "lease_expired"
            else:
                raise attachment_error("ATTACHMENT_LEASE_CONCURRENT", 409)
        lease = SessionAttachmentLease(
            id=payload.lease_id,
            session_id=runtime.id,
            user_id=runtime.user_id,
            node_id=node.id,
            device_id=device.id,
            ssh_key_id=key.id,
            runtime_backend=runtime.runtime_backend,
            runtime_resource_id=runtime.runtime_resource_id,
            runtime_scope=str(uuid4()),
            status="issued",
            expires_at=now + timedelta(seconds=LEASE_SECONDS),
            max_bytes=MAX_BYTES,
            max_entries=MAX_ENTRIES,
            used_bytes=0,
            used_entries=0,
        )
        self.session.add(lease)
        self.session.add(
            AuditLog(
                actor_user_id=None,
                action="node_api.attach_exact_attachment_admit",
                target_type="session_attachment_lease",
                target_id=str(lease.id),
                details={
                    "node_id": str(node.id),
                    "session_id": str(runtime.id),
                    "device_id": str(device.id),
                },
            )
        )
        await self.session.commit()
        return SessionAttachmentAdmissionData.model_validate(
            {
                "node_id": lease.node_id,
                "session_id": lease.session_id,
                "device_id": lease.device_id,
                "ssh_key_id": lease.ssh_key_id,
                "lease_id": lease.id,
                "runtime_backend": lease.runtime_backend,
                "runtime_resource_id": lease.runtime_resource_id,
                "runtime_scope": lease.runtime_scope,
                "expires_at": lease.expires_at,
                "max_bytes": lease.max_bytes,
                "max_entries": lease.max_entries,
            }
        )

    async def record_cleanup(
        self, node: Node, payload: SessionAttachmentCleanupRequest
    ) -> SessionAttachmentCleanupRequest:
        """
        校验 Node 返回的回执并以租约为键幂等持久化。

        :param node (Node): 已认证节点
        :param payload (SessionAttachmentCleanupRequest): Node 清理回执
        :return SessionAttachmentCleanupRequest: 持久化后的公开回执
        :raises ApiError: 回执身份冲突、重放或不符合生命周期
        """

        self._require_uuid(payload.node_id, "node_id")
        self._require_uuid(payload.session_id, "session_id")
        self._require_uuid(payload.device_id, "device_id")
        self._require_uuid(payload.ssh_key_id, "ssh_key_id")
        self._require_uuid(payload.lease_id, "lease_id")
        self._require_uuid(payload.receipt_id, "receipt_id")
        if node.id != payload.node_id:
            raise attachment_error("ATTACHMENT_NODE_MISMATCH", 403)
        lease = await self.session.scalar(
            select(SessionAttachmentLease)
            .where(SessionAttachmentLease.id == payload.lease_id)
            .with_for_update()
        )
        if lease is None:
            raise attachment_error("ATTACHMENT_LEASE_NOT_FOUND", 404)
        if (
            lease.node_id != node.id
            or lease.session_id != payload.session_id
            or lease.device_id != payload.device_id
            or lease.ssh_key_id != payload.ssh_key_id
        ):
            raise attachment_error("ATTACHMENT_IDENTITY_CHANGED", 403)
        receipt = await self.session.scalar(
            select(SessionAttachmentCleanupReceipt)
            .where(SessionAttachmentCleanupReceipt.lease_id == lease.id)
            .with_for_update()
        )
        if receipt is not None:
            if receipt.receipt_id != payload.receipt_id:
                raise attachment_error("ATTACHMENT_RECEIPT_REPLAY", 409)
            if receipt.status == "cleaned" or receipt.status == payload.status:
                await self.session.commit()
                return _receipt_data(receipt)
            if receipt.status not in {"pending", "unknown"} or payload.status != "cleaned":
                raise attachment_error("ATTACHMENT_RECEIPT_CONFLICT", 409)
        valid = lease.status in {
            "issued",
            "cleanup_pending",
        } and await self._current_identity_is_valid(lease)
        now = datetime.now(UTC)
        status = payload.status if valid and _aware(lease.expires_at) > now else "rejected"
        if receipt is None:
            receipt = SessionAttachmentCleanupReceipt(
                id=uuid4(),
                lease_id=lease.id,
                receipt_id=payload.receipt_id,
                session_id=lease.session_id,
                user_id=lease.user_id,
                node_id=lease.node_id,
                device_id=lease.device_id,
                ssh_key_id=lease.ssh_key_id,
                status=status,
                removed_batches=payload.removed_batches,
                observed_at=now,
            )
            self.session.add(receipt)
        else:
            receipt.status = status
            receipt.removed_batches = payload.removed_batches
            receipt.observed_at = now
        lease.status = {
            "cleaned": "cleaned",
            "pending": "cleanup_pending",
            "unknown": "cleanup_pending",
            "rejected": "rejected",
        }[status]
        if status == "cleaned":
            lease.cleaned_at = now
        elif status == "rejected":
            lease.invalidated_at = lease.invalidated_at or now
            lease.invalidation_reason = lease.invalidation_reason or "cleanup_rejected"
        await self.session.commit()
        return _receipt_data(receipt)

    async def invalidate_for_session(self, session_id: UUID, reason: str) -> int:
        """
        在会话删除前撤销其全部未完成附件租约。

        :param session_id (UUID): 原工具会话标识
        :param reason (str): 有界失效原因
        :return int: 被撤销租约数量
        """

        return await self._invalidate(SessionAttachmentLease.session_id == session_id, reason)

    async def invalidate_for_device(self, device_id: UUID, reason: str) -> int:
        """
        在设备撤销事务内撤销其全部未完成附件租约。

        :param device_id (UUID): 本地设备标识
        :param reason (str): 有界失效原因
        :return int: 被撤销租约数量
        """

        return await self._invalidate(SessionAttachmentLease.device_id == device_id, reason)

    async def _invalidate(self, predicate: ColumnElement[bool], reason: str) -> int:
        """
        锁定并更新指定范围内的活动租约。

        :param predicate (ColumnElement[bool]): SQLAlchemy 范围条件
        :param reason (str): 有界失效原因
        :return int: 更新数量
        """

        now = datetime.now(UTC)
        leases = list(
            await self.session.scalars(
                select(SessionAttachmentLease)
                .where(predicate)
                .where(SessionAttachmentLease.status.in_(LIVE_LEASE_STATES))
                .with_for_update()
            )
        )
        for lease in leases:
            lease.status = "revoked"
            lease.invalidated_at = now
            lease.invalidation_reason = reason[:64]
        return len(leases)

    async def _current_identity_is_valid(self, lease: SessionAttachmentLease) -> bool:
        """
        清理时重新检查会话、设备和 SSH 密钥的当前所有权。

        :param lease (SessionAttachmentLease): 原始租约
        :return bool: 当前身份仍可使用时为真
        """

        runtime = await self.session.get(Session, lease.session_id)
        if (
            runtime is None
            or runtime.user_id != lease.user_id
            or runtime.node_id != lease.node_id
            or runtime.tool_type != "claude"
            or runtime.status not in {"running", "active"}
            or runtime.runtime_backend != lease.runtime_backend
        ):
            return False
        device = await self.session.scalar(
            select(UserDevice).where(
                UserDevice.id == lease.device_id,
                UserDevice.user_id == lease.user_id,
                UserDevice.status == "active",
            )
        )
        if device is None:
            return False
        key = await self.session.scalar(
            select(SshKey).where(
                SshKey.id == lease.ssh_key_id,
                SshKey.user_device_id == lease.device_id,
                SshKey.status == "active",
            )
        )
        return key is not None

    @staticmethod
    def _require_uuid(value: UUID, field: str) -> None:
        """
        拒绝零 UUID，防止占位符混入身份合同。

        :param value (UUID): 待检查 UUID
        :param field (str): 字段名
        """

        if value.int == 0:
            raise attachment_error("ATTACHMENT_INVALID_IDENTITY", 422)

    @staticmethod
    def _require_capability(node: Node) -> None:
        """
        只接受当前心跳明确声明的 v1 传输能力。

        :param node (Node): 待验证节点
        """

        capability = node.runtime_capabilities.get("session_attachment_transfer")
        if (
            not isinstance(capability, dict)
            or capability.get("supported") is not True
            or type(capability.get("protocol_version")) is not int
            or capability.get("protocol_version") != 1
        ):
            raise attachment_error("NODE_SESSION_ATTACHMENT_TRANSFER_UNSUPPORTED")


def _aware(value: datetime) -> datetime:
    """
    将 SQLite 返回的无时区时间统一解释为 UTC。

    :param value (datetime): 数据库时间
    :return datetime: 带 UTC 时区时间
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _receipt_data(receipt: SessionAttachmentCleanupReceipt) -> SessionAttachmentCleanupRequest:
    """
    将持久化回执转换为严格公开结构。

    :param receipt (SessionAttachmentCleanupReceipt): 回执实体
    :return SessionAttachmentCleanupRequest: 公开回执
    """

    return SessionAttachmentCleanupRequest.model_validate(
        {
            "node_id": receipt.node_id,
            "session_id": receipt.session_id,
            "device_id": receipt.device_id,
            "ssh_key_id": receipt.ssh_key_id,
            "lease_id": receipt.lease_id,
            "status": receipt.status,
            "removed_batches": receipt.removed_batches,
            "receipt_id": receipt.receipt_id,
        }
    )


def attachment_error(code: str, status: int = 409) -> ApiError:
    """
    返回不泄露路径、账户或密钥的固定附件错误。

    :param code (str): 稳定错误码
    :param status (int): HTTP 状态码
    :return ApiError: 固定拒绝错误
    """

    return ApiError(
        code=code, message="Exact session attachment transfer is unavailable.", status_code=status
    )
