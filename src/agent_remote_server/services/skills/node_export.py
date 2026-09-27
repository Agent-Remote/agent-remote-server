"""
为原始冻结快照签发和重验只读 SSH 导出权限，不依赖内容上传配额。
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask
from agent_remote_server.repositories.connections import ConnectionRepository
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.skill_node_export import ExportRows, NodeExportRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_node_export import (
    NodeExportAuthorization,
    NodeExportBinding,
    NodeExportPermission,
    NodeExportRenewal,
    NodeExportRequest,
    NodeExportVerification,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.node_export_tokens import NodeExportGrant, NodeExportTokens
from agent_remote_server.services.ssh_keys import ssh_key_sync_payload, ssh_key_sync_task_id


class NodeExportService:
    """
    原用户授权与强制命令设备身份同时有效才允许读取，所有失败均不回显凭据。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        绑定请求事务及部署签名策略。

        :param session (AsyncSession): 当前请求会话
        :param settings (Settings): 当前部署配置
        """
        self.session = session
        self.settings = settings
        self.repository = NodeExportRepository(session)
        self.tokens = NodeExportTokens(settings.secret_key)

    async def authorize(
        self, user_id: UUID, token_id: UUID, snapshot_id: UUID, request: NodeExportRequest
    ) -> NodeExportAuthorization:
        """
        为已有快照和设备签发固定授权，仅确保既有 SSH 公钥同步任务。

        :param user_id (UUID): 已认证用户
        :param token_id (UUID): 原始用户访问令牌身份
        :param snapshot_id (UUID): 指定原快照
        :param request (NodeExportRequest): 精确设备和公钥
        :return NodeExportAuthorization: 不证明本地冻结存在的短期连接信息
        """
        self._enabled()
        now = datetime.now(UTC)
        token = await self.repository.identity(
            user_id, token_id, request.device_id, request.ssh_key_id, now
        )
        if token is None:
            raise self._denied()
        # 快照已有存储用户锁，复用它序列化同设备的幂等密钥任务，但不创建用量或内容。
        if not await SkillStorageRepository(self.session).lock_existing_usage_for_mutation(user_id):
            raise self._denied()
        now = datetime.now(UTC)
        token = await self.repository.identity(
            user_id, token_id, request.device_id, request.ssh_key_id, now
        )
        if token is None:
            raise self._denied()
        rows = await self.repository.snapshot(user_id, snapshot_id)
        binding = export_binding(rows)
        node = await self.repository.node(binding.node_id)
        if node is None or node.status not in {"healthy", "degraded", "maintenance"}:
            raise SkillContentError("STATE_EXPORT_UNAVAILABLE", "source Node is unavailable")
        host, username = node.wireguard_ip or node.ssh_host, node.ssh_user or "agent-remote"
        if (
            host is None
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,254}", host)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", username)
            or not 1 <= (node.ssh_port or 22) <= 65535
        ):
            raise SkillContentError(
                "STATE_EXPORT_UNAVAILABLE", "source Node connection is unavailable"
            )
        task = await self._synchronize_key(binding.node_id, request.device_id, username)
        expiry = (
            token.expires_at.replace(tzinfo=UTC)
            if token.expires_at.tzinfo is None
            else token.expires_at
        )
        issued = int(now.timestamp())
        deadline = min(issued + 900, int(expiry.timestamp()))
        if deadline <= issued:
            raise self._denied()
        grant = NodeExportGrant(
            grant_id=uuid4(),
            token_id=token.id,
            device_id=request.device_id,
            ssh_key_id=request.ssh_key_id,
            binding=binding,
            issued_at=issued,
            expires_at=deadline,
        )
        return NodeExportAuthorization(
            binding=binding,
            device_id=request.device_id,
            ssh_key_id=request.ssh_key_id,
            grant=self.tokens.sign(grant),
            expires_at=datetime.fromtimestamp(deadline, UTC),
            ssh_host=host,
            ssh_port=node.ssh_port or 22,
            ssh_user=username,
            authorization_task_id=task.task_id,
            authorization_task_status=cast(
                Literal["pending", "leased", "running", "succeeded"], task.status
            ),
        )

    async def verify(
        self, node_id: UUID, snapshot_id: UUID, request: NodeExportVerification
    ) -> NodeExportPermission:
        """
        原节点在线校验授权及原始身份，捕获事实只能来自已有收尾观察和本地 Helper。

        :param node_id (UUID): 当前认证节点
        :param snapshot_id (UUID): 强制命令所选原快照
        :param request (NodeExportVerification): 原授权及可信 SSH 身份
        :return NodeExportPermission: 当前仍有效的精确读取许可
        """
        _, permission, _ = await self._verify(node_id, snapshot_id, request)
        return permission

    async def renew(
        self, node_id: UUID, snapshot_id: UUID, request: NodeExportVerification
    ) -> NodeExportRenewal:
        """
        仅对仍活跃的原凭据续签短期读取权限，原用户令牌和快照身份保持不变。

        :param node_id (UUID): 当前认证的原来源节点
        :param snapshot_id (UUID): 原始快照身份
        :param request (NodeExportVerification): 仍有效的原凭据及可信 SSH 身份
        :return NodeExportRenewal: 与请求精确关联的短期后继凭据
        """
        grant, permission, expiry = await self._verify(node_id, snapshot_id, request)
        issued = int(datetime.now(UTC).timestamp())
        # 数据库读取可能越过旧截止时刻，不能把已过期的输入重新变为有效凭据。
        self.tokens.verify(request.grant, issued)
        deadline = min(issued + 900, int(expiry.timestamp()))
        if deadline <= issued:
            raise self._denied()
        successor = grant.model_copy(update={"issued_at": issued, "expires_at": deadline})
        return NodeExportRenewal(
            previous_grant_digest=hashlib.sha256(request.grant.encode("utf-8")).hexdigest(),
            grant=self.tokens.sign(successor),
            permission=permission.model_copy(
                update={"expires_at": datetime.fromtimestamp(deadline, UTC)}
            ),
        )

    async def _verify(
        self, node_id: UUID, snapshot_id: UUID, request: NodeExportVerification
    ) -> tuple[NodeExportGrant, NodeExportPermission, datetime]:
        """
        共用完整在线权限检查，并保留限制续签所需的原用户令牌截止时刻。

        :param node_id (UUID): 当前认证节点
        :param snapshot_id (UUID): 原始快照身份
        :param request (NodeExportVerification): 当前凭据及可信 SSH 身份
        :return tuple[NodeExportGrant, NodeExportPermission, datetime]: 原凭据、许可及令牌期限
        """
        self._enabled()
        now = datetime.now(UTC)
        grant = self.tokens.verify(request.grant, int(now.timestamp()))
        if (
            grant.binding.node_id != node_id
            or grant.binding.snapshot_id != snapshot_id
            or grant.device_id != request.device_id
            or grant.ssh_key_id != request.ssh_key_id
        ):
            raise self._denied()
        token = await self.repository.identity(
            grant.binding.user_id, grant.token_id, grant.device_id, grant.ssh_key_id, now
        )
        if token is None:
            raise self._denied()
        rows = await self.repository.snapshot(grant.binding.user_id, snapshot_id)
        binding = export_binding(rows)
        if rows is None or binding != grant.binding:
            raise self._denied()
        _, termination, finalization = rows
        if (
            termination is not None
            and finalization is not None
            and (
                termination.incoming_digest is not None
                and termination.incoming_digest != finalization.incoming_digest
                or termination.unclean != finalization.unclean
            )
        ):
            raise self._denied()
        observation = finalization if finalization is not None else termination
        permission = NodeExportPermission(
            binding=binding,
            device_id=grant.device_id,
            ssh_key_id=grant.ssh_key_id,
            expires_at=datetime.fromtimestamp(grant.expires_at, UTC),
            incoming_digest=observation.incoming_digest if observation is not None else None,
            unclean=observation.unclean if observation is not None else None,
        )
        expiry = (
            token.expires_at.replace(tzinfo=UTC)
            if token.expires_at.tzinfo is None
            else token.expires_at
        )
        return grant, permission, expiry

    async def _synchronize_key(self, node_id: UUID, device_id: UUID, ssh_user: str) -> NodeTask:
        """
        复用已有强制命令同步协议，只重启未完成的原密钥任务。

        :param node_id (UUID): 原来源节点
        :param device_id (UUID): 已验证设备
        :param ssh_user (str): 节点受限用户
        :return NodeTask: 确切的既有或新同步任务
        """
        keys = await ConnectionRepository(self.session).list_active_ssh_keys_for_device(device_id)
        identity = ssh_key_sync_task_id(node_id=node_id, device_id=device_id, ssh_keys=keys)
        payload = ssh_key_sync_payload(device_id=device_id, ssh_user=ssh_user, ssh_keys=keys)
        repository = NodeRepository(self.session)
        task = await repository.get_task_by_task_id(identity)
        if task is None:
            return await repository.add_task(
                NodeTask(
                    node_id=node_id,
                    task_id=identity,
                    task_type="sync_ssh_keys",
                    status="pending",
                    payload=payload,
                )
            )
        if task.node_id != node_id or task.task_type != "sync_ssh_keys" or task.payload != payload:
            raise self._denied()
        if task.status in {"failed", "cancelled", "expired"}:
            task.status, task.lease_until = "pending", None
        if task.status not in {"pending", "leased", "running", "succeeded"}:
            raise self._denied()
        return task

    def _enabled(self) -> None:
        """
        与已有技能读取遵守相同的显式功能开关。
        """
        if not self.settings.skill_manager_enabled:
            raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")

    @staticmethod
    def _denied() -> SkillContentError:
        """
        对所有未授权身份返回相同的无内容诊断。

        :return SkillContentError: 不包含私有身份或凭据的失败
        """
        return SkillContentError("STATE_EXPORT_DENIED", "frozen export authorization denied")


def export_binding(rows: ExportRows | None) -> NodeExportBinding:
    """
    从永久历史行提取精确身份，不查询当前账户配置或启动准备内容。

    :param rows (ExportRows | None): 同所有者授权的原快照观察
    :return NodeExportBinding: 保留的原始准备绑定
    """
    if rows is None:
        raise SkillContentError("STATE_EXPORT_DENIED", "frozen export authorization denied")
    snapshot = rows[0]
    if snapshot.runtime_backend != "native" or snapshot.prepare_task_id is None:
        raise SkillContentError("STATE_EXPORT_UNAVAILABLE", "original snapshot cannot be exported")
    if snapshot.content_retired_at is not None:
        raise SkillContentError("STATE_EXPIRED", "original snapshot metadata is retired")
    return NodeExportBinding(
        snapshot_id=snapshot.id,
        session_id=snapshot.session_reference_id,
        user_id=snapshot.user_id,
        account_id=snapshot.account_id,
        node_id=snapshot.node_id,
        task_id=snapshot.prepare_task_id,
        library_generation=snapshot.library_generation,
        directory_epoch=snapshot.directory_epoch,
        initial_tree_digest=snapshot.tree_digest,
    )
