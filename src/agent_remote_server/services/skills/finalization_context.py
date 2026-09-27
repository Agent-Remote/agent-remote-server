"""
统一收尾请求的节点授权、活跃尝试校验及稳定状态响应。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.models.skill_transfers import SkillFinalizationTransfer
from agent_remote_server.repositories.skill_finalization import SkillFinalizationRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_finalizations import SkillFinalizationView
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService


@dataclass
class FinalizationContext:
    """
    同一请求事务的收尾授权依赖，不缓存跨请求权限。
    """

    repository: SkillFinalizationRepository
    runtime: SkillRuntimeRepository
    storage: SkillStorageRepository
    content: SkillContentService

    async def snapshot(self, node_id: UUID, snapshot_id: UUID) -> SessionSkillSnapshot:
        """
        用户锁内重新检查终态，不把已停止展示状态当作另一节点的权限。

        :param node_id (UUID): 认证节点
        :param snapshot_id (UUID): 精确快照
        :return SessionSkillSnapshot: 允许保存收尾内容的快照
        """
        snapshot = await self.repository.snapshot(node_id, snapshot_id)
        if snapshot is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        await self.storage.lock_usage(snapshot.user_id)
        snapshot = await self.repository.snapshot(node_id, snapshot_id)
        if snapshot is None or snapshot.status == "cancelled":
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        if snapshot.session_id is None:
            if snapshot.status != "retained":
                raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        else:
            session = await self.runtime.session(snapshot.user_id, snapshot.session_id)
            if session is None or session.status not in {"stopped", "interrupted", "failed"}:
                raise SkillContentError(
                    "STATE_WRITERS_ACTIVE", "session has not reached a stopped state"
                )
        return snapshot

    async def receipt(
        self, node_id: UUID, finalization_id: UUID
    ) -> tuple[SkillFinalization, SessionSkillSnapshot]:
        """
        收尾查询与上传同样重新验证快照身份及终态。

        :param node_id (UUID): 认证节点
        :param finalization_id (UUID): 收尾身份
        :return tuple[SkillFinalization, SessionSkillSnapshot]: 已授权收尾与原快照
        """
        receipt = await self.repository.receipt(node_id, finalization_id)
        if receipt is None:
            raise SkillContentError("FINALIZATION_NOT_FOUND", "finalization not found")
        snapshot = await self.snapshot(node_id, receipt.snapshot_id)
        receipt = await self.repository.receipt(node_id, finalization_id)
        if receipt is None:
            raise SkillContentError("FINALIZATION_NOT_FOUND", "finalization not found")
        return receipt, snapshot

    async def transfer(
        self,
        receipt: SkillFinalization,
        upload_id: UUID | None = None,
        *,
        metadata_only: bool = False,
    ) -> tuple[SkillFinalizationTransfer, SkillContentUpload]:
        """
        只允许当前绑定的上传尝试，已替换租约不会恢复权限。

        :param receipt (SkillFinalization): 已授权收尾
        :param upload_id (UUID | None): 写入或完成操作明确提供的尝试身份
        :param metadata_only (bool): 单文件请求只检查当前租约，避免触发全用户扫描
        :return tuple[SkillFinalizationTransfer, SkillContentUpload]: 当前绑定及租约
        """
        transfer = await self.repository.transfer(receipt)
        if transfer is None:
            raise SkillContentError(
                "STATE_TRANSFER_UNAVAILABLE", "finalization transfer is unavailable"
            )
        if upload_id is not None and transfer.upload_id != upload_id:
            raise SkillContentError("UPLOAD_SUPERSEDED", "upload attempt has been replaced")
        if metadata_only:
            upload = await self.content.inspect_upload(receipt.user_id, transfer.upload_id)
        else:
            upload = await self.content.get(receipt.user_id, transfer.upload_id)
        return transfer, upload

    async def view(self, receipt: SkillFinalization) -> SkillFinalizationView:
        """
        原始输入与上传尝试状态分开返回，避免单文件成功冒充完成。

        :param receipt (SkillFinalization): 已授权收尾
        :return SkillFinalizationView: 可持久化的完整回执
        """
        transfer, upload = await self.transfer(receipt)
        return SkillFinalizationView.model_validate(
            {
                "id": receipt.id,
                "snapshot_id": receipt.snapshot_id,
                "incoming_digest": receipt.incoming_digest,
                "unclean": receipt.unclean,
                "status": receipt.status,
                "checkpoint_id": receipt.checkpoint_id,
                "upload_id": upload.id,
                "upload_attempt": transfer.attempt,
                "upload_status": upload.status,
                "expires_at": upload.expires_at,
            }
        )
