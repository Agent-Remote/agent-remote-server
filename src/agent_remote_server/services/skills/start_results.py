"""
在通用任务结果事务内校验受管启动的固定快照和原始领取轮次。
"""

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_start_result import (
    ManagedStartIdentity,
    ManagedStartObservation,
    ManagedStartReady,
    ManagedStartStopped,
)
from agent_remote_server.services.skills.content import SkillContentError


def managed_start_task(task: NodeTask) -> bool:
    """
    非规范或空指针也不能绕过受管完成校验。

    :param task (NodeTask): 当前认证节点的任务
    :return bool: 是否携带任意受管标记
    """
    return task.task_type == "create_tool_session" and any(
        key.casefold() == "skill_manager" for key in task.payload
    )


class ManagedStartResultGuard:
    """
    复用外层保留时钟和任务结果事务，原始收据重放不再应用生命周期副作用。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定调用方负责提交的异步事务。

        :param session (AsyncSession): 节点回报事务
        """
        self._runtime = SkillRuntimeRepository(session)
        self._storage = SkillStorageRepository(session)
        self._nodes = NodeRepository(session)

    async def authorize(
        self, task: NodeTask, result: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        锁定并核验原始授权，同事务推进快照阶段，返回是否已经提交相同结果。

        :param task (NodeTask): 已绑定认证节点的逻辑任务
        :param result (dict[str, object]): 精确有界结果
        :param outcome (Literal["succeeded", "failed"]): 完成或停止失败类型
        :return bool: 相同历史收据是否只需重放
        """
        if not managed_start_task(task) and not await self._runtime.has_task_snapshot(
            task.node_id, task.id
        ):
            return False
        try:
            payload = (
                ManagedStartReady.model_validate(result)
                if outcome == "succeeded"
                else ManagedStartStopped.model_validate(result)
            )
        except ValidationError as exc:
            raise SkillContentError(
                "SKILL_START_RESULT_INVALID", "invalid managed startup result"
            ) from exc
        if payload.model_dump(mode="json") != result or payload.task_record_id != task.id:
            raise SkillContentError("SKILL_START_RESULT_INVALID", "invalid managed startup result")
        locked, snapshot, runtime = await self._context(task, payload)
        if await self._replay(locked, result, outcome):
            return True
        _require_live_start_lease(locked, snapshot, runtime, payload)
        snapshot.status = "started" if outcome == "succeeded" else "finalizing"
        return False

    async def inspect(
        self, task: NodeTask, payload: ManagedStartReady | ManagedStartStopped
    ) -> ManagedStartObservation:
        """
        在完成回报的同一锁序内只读观察，旧轮次不能在新轮次的缺席证据之后提交。

        :param task (NodeTask): 已绑定认证节点的原始逻辑任务
        :param payload (ManagedStartReady | ManagedStartStopped): 待确认的精确原始回报
        :return ManagedStartObservation: 不含租约授权的原始收据观察
        """
        if payload.task_record_id != task.id:
            raise SkillContentError("SKILL_START_RESULT_INVALID", "invalid managed startup result")
        locked, _, _ = await self._context(task, payload)
        outcome: Literal["succeeded", "failed"] = (
            "succeeded" if isinstance(payload, ManagedStartReady) else "failed"
        )
        accepted = await self._replay(locked, payload.model_dump(mode="json"), outcome)
        if not accepted and locked.status in {"succeeded", "failed"}:
            raise SkillContentError(
                "SKILL_START_RESULT_CONFLICT", "managed startup terminal receipt is unavailable"
            )
        return ManagedStartObservation.model_validate(
            {
                "result": payload,
                "accepted": accepted,
                "current_lease_attempt": locked.retry_count,
                "task_status": locked.status,
            }
        )

    async def _context(
        self, task: NodeTask, payload: ManagedStartIdentity
    ) -> tuple[NodeTask, SessionSkillSnapshot, Session | None]:
        """
        按用户、会话和任务的既定顺序重新核验固定身份，不授予新租约。

        :param task (NodeTask): 认证节点的原始任务
        :param payload (ManagedStartIdentity): 精确回报身份
        :return tuple[NodeTask, SessionSkillSnapshot, Session | None]: 锁内任务、快照和当前会话
        """
        snapshot = await self._runtime.node_snapshot(
            task.node_id, payload.skill_snapshot_id, task.id
        )
        if snapshot is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        if await self._storage.lock_existing_usage(snapshot.user_id) is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        snapshot = await self._runtime.node_snapshot(
            task.node_id, payload.skill_snapshot_id, task.id
        )
        if snapshot is None:
            raise SkillContentError("SNAPSHOT_NOT_FOUND", "snapshot not found")
        runtime = (
            await self._runtime.session(snapshot.user_id, snapshot.session_id)
            if snapshot.session_id is not None
            else None
        )
        locked = await self._runtime.task(task.node_id, task.id)
        if locked is None or not managed_start_task(locked):
            raise SkillContentError(
                "SKILL_START_AUTHORITY_CHANGED", "managed startup authority changed"
            )
        _validate_start_binding(locked, snapshot, payload)
        return locked, snapshot, runtime

    async def _replay(
        self, locked: NodeTask, result: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        只接受同一原始终态收据，不能把不同结果当成缺席或覆盖依据。

        :param locked (NodeTask): 锁定的原始任务
        :param result (dict[str, object]): 精确原始有界回报
        :param outcome (Literal["succeeded", "failed"]): 回报对应任务终态
        :return bool: 是否已提交相同收据
        """
        saved = await self._nodes.get_task_result(locked.task_id)
        if saved is not None:
            original = saved.result if outcome == "succeeded" else saved.error
            if (
                saved.node_task_id != locked.id
                or saved.status != outcome
                or locked.status != outcome
                or original != result
            ):
                raise SkillContentError(
                    "SKILL_START_RESULT_CONFLICT", "managed startup result already differs"
                )
            return True
        return False


def _validate_start_binding(
    task: NodeTask, snapshot: SessionSkillSnapshot, payload: ManagedStartIdentity
) -> None:
    """
    核对不可替换的任务指针、原始归属和中立资源身份。

    :param task (NodeTask): 锁定的当前任务
    :param snapshot (SessionSkillSnapshot): 原始快照
    :param payload (ManagedStartIdentity): 类型化启动结果
    """
    pointer = task.payload.get("skill_manager")
    expected_pointer = {
        "protocol_version": 1,
        "manifest_version": 1,
        "snapshot_id": str(snapshot.id),
        "task_id": str(task.id),
    }
    owners = {
        "session_id": str(snapshot.session_reference_id),
        "tool_account_id": str(snapshot.account_id),
        "user_id": str(snapshot.user_id),
        "runtime_backend": "native",
        "tool_type": "claude",
    }
    unit = (
        "agent-remote-session-"
        + hashlib.sha256(str(snapshot.session_reference_id).encode()).hexdigest()[:12]
        + ".service"
    )
    if (
        not isinstance(pointer, dict)
        or pointer != expected_pointer
        or any(
            type(pointer.get(key)) is not int for key in ("protocol_version", "manifest_version")
        )
        or any(key != "skill_manager" and key.casefold() == "skill_manager" for key in task.payload)
        or any(task.payload.get(key) != value for key, value in owners.items())
        or snapshot.runtime_backend != "native"
        or payload.session_id != snapshot.session_reference_id
        or payload.tool_account_id != snapshot.account_id
        or payload.runtime_resource_id != unit
        or isinstance(payload, ManagedStartReady)
        and payload.tmux_session_name != task.payload.get("tmux_session_name")
    ):
        raise SkillContentError(
            "SKILL_START_AUTHORITY_CHANGED", "managed startup authority changed"
        )


def _require_live_start_lease(
    task: NodeTask,
    snapshot: SessionSkillSnapshot,
    runtime: Session | None,
    payload: ManagedStartIdentity,
) -> None:
    """
    初次提交必须仍由当前领取轮次拥有尚未完成的原始会话。

    :param task (NodeTask): 锁定任务
    :param snapshot (SessionSkillSnapshot): 原始快照
    :param runtime (Session | None): 锁定的会话
    :param payload (ManagedStartIdentity): 含精确轮次的结果
    """
    deadline = task.lease_until
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if (
        snapshot.status != "reserved"
        or runtime is None
        or runtime.status != "starting"
        or runtime.node_id != snapshot.node_id
        or runtime.tool_account_id != snapshot.account_id
        or runtime.runtime_backend != snapshot.runtime_backend
        or runtime.tool_type != "claude"
        or task.status not in {"leased", "running"}
        or task.retry_count != payload.lease_attempt
        or deadline is None
        or deadline <= datetime.now(UTC)
    ):
        raise SkillContentError(
            "SKILL_START_LEASE_LOST", "managed startup lease is no longer valid"
        )
