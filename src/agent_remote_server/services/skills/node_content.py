"""
通过精确快照及有效准备租约约束节点内容读取。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule
from agent_remote_server.schemas.skill_snapshot_lease import (
    SkillSnapshotLease,
    SkillSnapshotLeaseRequest,
)
from agent_remote_server.schemas.skill_snapshots import SkillSnapshotItemView, SkillSnapshotView
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass(frozen=True)
class SnapshotFileDownload:
    """
    固定短事务授权的文件成员，复制后仍须重新验证快照权限。
    """

    user_id: UUID
    entry: SkillTreeEntry


class NodeSkillContentService:
    """
    不接受用户或树摘要作为授权，全部内容身份由快照导出。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        绑定当前请求的异步事务和私有存储。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 配额策略
        """
        self._store = store
        self._runtime = SkillRuntimeRepository(session)
        self._storage = SkillStorageRepository(session)
        self._content = SkillContentService(session, store, policy)

    async def authorize(
        self, node_id: UUID, snapshot_id: UUID, task_id: UUID
    ) -> SessionSkillSnapshot:
        """
        每次请求重新检查快照、用户、会话和租约，失败不泄露其他归属。

        :param node_id (UUID): 已认证节点
        :param snapshot_id (UUID): 请求快照
        :param task_id (UUID): 请求准备任务
        :return SessionSkillSnapshot: 当前仍允许准备下载的快照
        """
        snapshot = await self._runtime.node_snapshot(node_id, snapshot_id, task_id)
        if snapshot is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        await self._storage.lock_usage(snapshot.user_id)
        snapshot = await self._runtime.node_snapshot(node_id, snapshot_id, task_id)
        if snapshot is None or snapshot.status != "reserved" or snapshot.session_id is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        session = await self._runtime.session(snapshot.user_id, snapshot.session_id)
        task = await self._runtime.task(node_id, task_id)
        if (
            session is None
            or session.status != "starting"
            or task is None
            or task.task_type != "create_tool_session"
            or task.status not in {"leased", "running"}
            or task.lease_until is None
        ):
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        lease = task.lease_until
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=UTC)
        expected = {
            "session_id": str(snapshot.session_reference_id),
            "user_id": str(snapshot.user_id),
            "tool_account_id": str(snapshot.account_id),
            "runtime_backend": snapshot.runtime_backend,
        }
        binding = task.payload.get("skill_manager")
        if binding is not None and (
            not isinstance(binding, dict)
            or any(
                type(binding.get(field)) is not int
                for field in ("protocol_version", "manifest_version")
            )
            or binding
            != {
                "protocol_version": 1,
                "manifest_version": 1,
                "snapshot_id": str(snapshot.id),
                "task_id": str(task.id),
            }
        ):
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        if (
            lease <= datetime.now(UTC)
            or session.runtime_backend != snapshot.runtime_backend
            or any(task.payload.get(key) != value for key, value in expected.items())
        ):
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        return snapshot

    async def describe(self, node_id: UUID, snapshot_id: UUID, task_id: UUID) -> SkillSnapshotView:
        """
        返回完整物化清单，不放宽为任意 checkpoint 子树读取。

        :param node_id (UUID): 已认证节点
        :param snapshot_id (UUID): 请求快照
        :param task_id (UUID): 请求准备任务
        :return SkillSnapshotView: 完整固定准备封套
        """
        snapshot = await self.authorize(node_id, snapshot_id, task_id)
        manifest = await self._content.read_tree(snapshot.user_id, "state", snapshot.tree_digest)
        items = await self._runtime.snapshot_items(snapshot)
        return SkillSnapshotView.model_validate(
            {
                "snapshot_id": snapshot.id,
                "user_id": snapshot.user_id,
                "account_id": snapshot.account_id,
                "node_id": snapshot.node_id,
                "session_id": snapshot.session_reference_id,
                "task_id": task_id,
                "runtime_backend": snapshot.runtime_backend,
                "library_generation": snapshot.library_generation,
                "directory_epoch": snapshot.directory_epoch,
                "starting_checkpoint_id": snapshot.starting_checkpoint_id,
                "tree_digest": snapshot.tree_digest,
                "manifest": manifest,
                "items": [
                    SkillSnapshotItemView(
                        state_id=item.state_id,
                        entry_name=item.entry_name,
                        state_epoch=item.state_epoch,
                        checkpoint_id=item.checkpoint_id,
                        resolution=ResolvedSkillRule.model_validate(item.resolution_json),
                    )
                    for item in items
                ],
                "system_releases": snapshot.system_releases_json,
            }
        )

    async def authorize_file(
        self, node_id: UUID, snapshot_id: UUID, task_id: UUID, digest: str
    ) -> SnapshotFileDownload:
        """
        在短事务中固定清单成员，不在数据库锁内等待磁盘复制。

        :param node_id (UUID): 已认证节点
        :param snapshot_id (UUID): 请求快照
        :param task_id (UUID): 请求准备任务
        :param digest (str): 请求文件摘要
        :return SnapshotFileDownload: 不含任意路径的固定文件成员
        """
        snapshot = await self.authorize(node_id, snapshot_id, task_id)
        entry = await self._content.authorize_file(
            snapshot.user_id, "state", snapshot.tree_digest, digest
        )
        return SnapshotFileDownload(snapshot.user_id, entry)

    async def copy_authorized_file(self, download: SnapshotFileDownload, target: BinaryIO) -> None:
        """
        将完整验证的字节写入私有暂存，成功不代表当前授权仍然有效。

        :param download (SnapshotFileDownload): 前一短事务固定的清单成员
        :param target (BinaryIO): 不会提前对外发布的私有输出
        """
        try:
            await self._store.copy_file(download.user_id, download.entry, target)
        except FileNotFoundError as error:
            raise SkillContentError(
                "CONTENT_INCOMPLETE", "retained content file is unavailable"
            ) from error
        except ValueError as error:
            raise SkillContentError(
                "CONTENT_INVALID", "retained content verification failed"
            ) from error

    async def renew_lease(
        self,
        node_id: UUID,
        snapshot_id: UUID,
        task_id: UUID,
        request: SkillSnapshotLeaseRequest,
        lease_seconds: int,
    ) -> SkillSnapshotLease:
        """
        仅延长原快照当前领取的有效租约，不创建新任务或变更固定输入。

        :param node_id (UUID): 已认证节点
        :param snapshot_id (UUID): 固定快照身份
        :param task_id (UUID): 原准备任务身份
        :param request (SkillSnapshotLeaseRequest): 当前领取轮次
        :param lease_seconds (int): 部署配置的短期租约秒数
        :return SkillSnapshotLease: 完整原始绑定和服务器短期预算
        """
        snapshot = await self.authorize(node_id, snapshot_id, task_id)
        task = await self._runtime.task(node_id, task_id)
        assert task is not None
        if not isinstance(task.payload.get("skill_manager"), dict):
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        if task.retry_count != request.lease_attempt:
            raise SkillContentError(
                "SNAPSHOT_LEASE_CHANGED", "task belongs to another poll attempt"
            )
        now = datetime.now(UTC)
        if (
            task.lease_until is None
            or task.lease_until.replace(tzinfo=task.lease_until.tzinfo or UTC) <= now
        ):
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        duration = min(lease_seconds, 300)
        if duration <= 0:
            raise SkillContentError("SNAPSHOT_LEASE_UNAVAILABLE", "task lease duration is invalid")
        task.lease_until = now + timedelta(seconds=duration)
        await self._runtime.flush()
        return SkillSnapshotLease.model_validate(
            {
                "snapshot_id": snapshot.id,
                "task_id": task.id,
                "node_id": node_id,
                "user_id": snapshot.user_id,
                "account_id": snapshot.account_id,
                "session_id": snapshot.session_reference_id,
                "runtime_backend": snapshot.runtime_backend,
                "lease_attempt": task.retry_count,
                "server_time": now,
                "lease_until": task.lease_until,
                "renew_after_milliseconds": max(1, duration * 1000 // 3),
            }
        )
