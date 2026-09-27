"""
独立提交原始会话终止观察，并在同一事务撤销活跃连接与迟到启动权限。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.ego_browser.relay import EgoBrowserRevocationPublisher
from agent_remote_server.models import AuditLog, NodeTask, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.repositories.skill_finalization import SkillFinalizationRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_terminations import (
    SkillCapturePendingRequest,
    SkillTerminationIdentity,
    SkillTerminationRequest,
)
from agent_remote_server.services.device_sessions import DeviceSessionService, RevokedDeviceBinding
from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation

type TerminationObservation = SkillTerminationRequest | SkillCapturePendingRequest


class SkillTerminationService:
    """
    停止确认不依赖启动租约，也不替代 Helper 的实际写入者退出证明。
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_hub: DeviceRelayHub,
        publisher: EgoBrowserRevocationPublisher,
    ) -> None:
        """
        复用现有事务锁和提交后连接撤销设施。

        :param session (AsyncSession): 当前请求事务
        :param settings (Settings): 部署配置
        :param relay_hub (DeviceRelayHub): 设备连接关闭设施
        :param publisher (EgoBrowserRevocationPublisher): 浏览器撤销发布设施
        """
        self._session = session
        self._runtime = SkillRuntimeRepository(session)
        self._repository = SkillFinalizationRepository(session)
        self._devices = DeviceSessionService(session, settings, relay_hub)
        self._browser = EgoBrowserService(session, settings, revocation_publisher=publisher)

    async def observe(
        self, node_id: UUID, snapshot_id: UUID, payload: TerminationObservation
    ) -> None:
        """
        固定首次输入并提交终态，精确重放不再次应用会话状态变更。

        :param node_id (UUID): 已认证原始节点
        :param snapshot_id (UUID): 原始固定快照
        :param payload (TerminationObservation): Helper 冻结输入观察
        """
        original = await self._runtime.node_snapshot(node_id, snapshot_id, payload.task_id)
        if original is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        revoked: tuple[RevokedDeviceBinding, ...] = ()
        async with retention_mutation(self._session, original.user_id):
            snapshot, runtime, task = await self._authorize(node_id, snapshot_id, payload)
            saved = await self._repository.termination(snapshot.id)
            finalization = await self._runtime.finalization(snapshot)
            incoming = (
                payload.incoming_digest if isinstance(payload, SkillTerminationRequest) else None
            )
            for record in (saved, finalization):
                if record is not None and (
                    record.unclean != payload.unclean
                    or incoming is not None
                    and record.incoming_digest is not None
                    and record.incoming_digest != incoming
                ):
                    raise SkillContentError(
                        "IDEMPOTENCY_CONFLICT", "termination input already differs"
                    )
            if saved is not None and saved.incoming_digest is None and incoming is not None:
                saved.incoming_digest, saved.capture_error = incoming, None
            if saved is None and incoming is None and finalization is not None:
                raise SkillContentError("IDEMPOTENCY_CONFLICT", "frozen input already recorded")
            if saved is None:
                if (
                    runtime is None
                    or snapshot.status == "cancelled"
                    or snapshot.content_retired_at is not None
                ):
                    raise SkillContentError(
                        "SNAPSHOT_NOT_ACTIVE", "snapshot cannot accept termination"
                    )
                stopped = await self._devices.stop_for_tool_session(
                    tool_session_id=runtime.id,
                    reason="skill_runtime_terminated",
                    audit_action="device_session.skill_termination",
                    commit=False,
                )
                revoked = stopped.revoked_bindings
                await self._browser.revoke_for_tool_session(
                    tool_session_id=runtime.id,
                    reason="skill_runtime_terminated",
                    commit=False,
                    publish=False,
                )
                self._retain(snapshot, runtime, task, payload)
        await self._session.commit()
        await self._devices.close_revoked_bindings(revoked)
        await self._browser.publish_pending_revocations()

    async def _authorize(
        self, node_id: UUID, snapshot_id: UUID, payload: SkillTerminationIdentity
    ) -> tuple[SessionSkillSnapshot, Session | None, NodeTask]:
        """
        用户锁内重新核对原始快照，再按会话和任务顺序加锁。

        :param node_id (UUID): 已认证节点
        :param snapshot_id (UUID): 精确快照
        :param payload (SkillTerminationIdentity): 原始观察身份
        :return tuple[SessionSkillSnapshot, Session | None, NodeTask]: 已锁定且匹配的原始对象
        """
        snapshot = await self._runtime.node_snapshot(node_id, snapshot_id, payload.task_id)
        if snapshot is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        runtime = (
            await self._runtime.session(snapshot.user_id, snapshot.session_id)
            if snapshot.session_id is not None
            else None
        )
        task = await self._runtime.task(node_id, payload.task_id)
        if task is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot task not found")
        validate_termination_binding(snapshot, task, payload)
        if runtime is not None and (
            runtime.node_id != node_id
            or runtime.tool_account_id != snapshot.account_id
            or runtime.runtime_backend != "native"
            or runtime.tool_type != "claude"
        ):
            raise SkillContentError("SNAPSHOT_BINDING_MISMATCH", "runtime identity differs")
        return snapshot, runtime, task

    def _retain(
        self,
        snapshot: SessionSkillSnapshot,
        runtime: Session,
        task: NodeTask,
        payload: TerminationObservation,
    ) -> None:
        """
        原子保留最小终止输入，保留已有启动结果和内容引用。

        :param snapshot (SessionSkillSnapshot): 原始快照
        :param runtime (Session): 原始会话
        :param task (NodeTask): 原始准备任务
        :param payload (TerminationObservation): 精确冻结输入
        """
        if runtime.status not in {"stopped", "interrupted", "failed"}:
            runtime.status = "interrupted" if payload.unclean else "stopped"
        if task.status in {"pending", "leased", "running"}:
            task.status, task.lease_until = "cancelled", None
        if snapshot.status in {"reserved", "started"}:
            snapshot.status = "finalizing"
        self._repository.add(
            SkillSnapshotTermination(
                snapshot_id=snapshot.id,
                incoming_digest=payload.incoming_digest
                if isinstance(payload, SkillTerminationRequest)
                else None,
                capture_error=payload.capture_error
                if isinstance(payload, SkillCapturePendingRequest)
                else None,
                unclean=payload.unclean,
            )
        )
        self._repository.add(
            AuditLog(
                actor_user_id=None,
                action="node_api.skill_termination",
                target_type="session",
                target_id=str(runtime.id),
                details={"snapshot_id": str(snapshot.id), "unclean": payload.unclean},
            )
        )


def validate_termination_binding(
    snapshot: SessionSkillSnapshot, task: NodeTask, payload: SkillTerminationIdentity
) -> None:
    """
    原始任务指针、所有者和准备代数不能被迟到请求替换。

    :param snapshot (SessionSkillSnapshot): 已授权快照
    :param task (NodeTask): 锁定的原始任务
    :param payload (SkillTerminationIdentity): 精确冻结身份
    """
    pointer = task.payload.get("skill_manager")
    owners = {
        "session_id": str(snapshot.session_reference_id),
        "user_id": str(snapshot.user_id),
        "tool_account_id": str(snapshot.account_id),
        "runtime_backend": "native",
        "tool_type": "claude",
    }
    if (
        snapshot.runtime_backend != "native"
        or task.task_type != "create_tool_session"
        or payload.session_id != snapshot.session_reference_id
        or payload.initial_tree_digest != snapshot.tree_digest
        or payload.directory_epoch != snapshot.directory_epoch
        or payload.library_generation != snapshot.library_generation
        or not isinstance(pointer, dict)
        or pointer
        != {
            "protocol_version": 1,
            "manifest_version": 1,
            "snapshot_id": str(snapshot.id),
            "task_id": str(task.id),
        }
        or any(
            type(pointer.get(key)) is not int for key in ("protocol_version", "manifest_version")
        )
        or any(key != "skill_manager" and key.casefold() == "skill_manager" for key in task.payload)
        or any(task.payload.get(key) != value for key, value in owners.items())
    ):
        raise SkillContentError(
            "SNAPSHOT_BINDING_MISMATCH", "original termination identity differs"
        )
