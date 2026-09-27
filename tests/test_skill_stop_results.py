"""
验证停止任务只接受原始终止证据，失败和重放不能制造保存完成。
"""

from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, NodeTask, NodeTaskResult, Session, User
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.sessions import ToolSessionService
from agent_remote_server.services.skills.content import SkillContentError


@pytest.mark.parametrize("capture_pending", [False, True])
@pytest.mark.parametrize("unclean", [False, True])
async def test_managed_stop_requires_termination_and_replay_keeps_original(
    stopped: RuntimeHarness, tmp_path: Path, unclean: bool, capture_pending: bool
) -> None:
    """
    停止身份在入队时固定，缺少冻结凭据、篡改结果和失败回报均不终结任务。

    :param stopped (RuntimeHarness): 已有原始快照的会话
    :param tmp_path (Path): 测试内容卷
    :param unclean (bool): 原始异常退出分类
    :param capture_pending (bool): 是否仅确认停止而尚未完成冻结
    """
    settings = Settings(
        secret_key="stop-result-test",
        skill_manager_enabled=True,
        skill_storage_root=tmp_path / "objects",
    )
    async with stopped.database.begin() as session:
        original = await session.get(Session, stopped.session)
        assert original is not None
        original.status = "running"
    async with stopped.database() as session:
        owner = await session.get(User, stopped.owner)
        assert owner is not None
        await ToolSessionService(session, settings).stop_session(
            user=owner, session_id=stopped.session
        )
    task_id = f"stop_tool_session:{stopped.session}"
    result: dict[str, object] = {
        "status": "stopped",
        "session_id": str(stopped.session),
        "runtime_backend": "native",
        "skill_finalization_operation_id": str(stopped.snapshot),
        "incoming_digest": None if capture_pending else "a" * 64,
        "unclean": unclean,
    }
    async with stopped.database() as session:
        node = await session.get(Node, stopped.node)
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        assert task is not None and node is not None
        assert task.payload["skill_finalization"] == {
            "snapshot_id": str(stopped.snapshot),
            "task_id": str(stopped.task),
            "user_id": str(stopped.owner),
            "account_id": str(stopped.account),
        }
        with pytest.raises(SkillContentError, match="not yet confirmed"):
            await NodeService(session, settings).complete_task(
                node=node, task_id=task_id, result=result
            )
    async with stopped.database.begin() as session:
        session.add(
            SkillSnapshotTermination(
                snapshot_id=stopped.snapshot,
                incoming_digest=None if capture_pending else "a" * 64,
                capture_error="quota_exceeded" if capture_pending else None,
                unclean=unclean,
            )
        )
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        assert task is not None
        task.payload = {
            key: value for key, value in task.payload.items() if key != "skill_finalization"
        }
    for outcome in ("failed", "changed"):
        async with stopped.database() as session:
            node = await session.get(Node, stopped.node)
            assert node is not None
            with pytest.raises(SkillContentError):
                if outcome == "failed":
                    await NodeService(session, settings).fail_task(
                        node=node, task_id=task_id, error={"code": "TEMPORARY"}
                    )
                else:
                    await NodeService(session, settings).complete_task(
                        node=node, task_id=task_id, result={**result, "unclean": not unclean}
                    )
    async with stopped.database() as session:
        node = await session.get(Node, stopped.node)
        assert node is not None
        await NodeService(session, settings).complete_task(
            node=node, task_id=task_id, result=result
        )
        original = await session.get(Session, stopped.session)
        assert original is not None and original.status == ("interrupted" if unclean else "stopped")
        if capture_pending:
            termination = await session.get(SkillSnapshotTermination, stopped.snapshot)
            assert termination is not None
            termination.incoming_digest, termination.capture_error = "a" * 64, None
        original.status = "failed"
        await session.commit()
        await NodeService(session, settings).complete_task(
            node=node, task_id=task_id, result=result
        )
        assert original.status == "failed"
        receipts = (
            await session.scalars(select(NodeTaskResult).where(NodeTaskResult.task_id == task_id))
        ).all()
        assert len(receipts) == 1 and receipts[0].result == result
        receipts[0].node_task_id = stopped.task
        await session.flush()
        with pytest.raises(SkillContentError, match="result changed"):
            await NodeService(session, settings).complete_task(
                node=node, task_id=task_id, result=result
            )


async def test_original_stop_task_cannot_lose_guard_through_payload_change(
    stopped: RuntimeHarness,
) -> None:
    """
    不可变逻辑停止身份仍指向受管快照，载荷移除或改指不能回退旧完成流程。

    :param stopped (RuntimeHarness): 原始快照身份
    """
    from agent_remote_server.services.skills.stop_results import ManagedStopResultGuard

    for value in (None, "invalid", str(stopped.node)):
        async with stopped.database() as session:
            task = NodeTask(
                node_id=stopped.node,
                task_type="stop_tool_session",
                task_id=f"stop_tool_session:{stopped.session}",
                status="running",
                payload={"session_id": value},
            )
            with pytest.raises(SkillContentError):
                await ManagedStopResultGuard(session).authorize(task, {}, "succeeded")
