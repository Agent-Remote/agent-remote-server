"""
防止通用运行时清单越过精确快照证据，误判受管启动或自然退出。
"""

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_snapshots import prepared as prepared
from test_skill_start_results import start_result
from test_skill_termination import termination_input

from agent_remote_server.models import NodeTask, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination


async def reconcile(
    client: AsyncClient, state: RuntimeHarness, reported: list[dict[str, object]]
) -> None:
    """
    通过认证路由提交不含原始调用身份的通用清单。

    :param client (AsyncClient): 原节点 HTTP 客户端
    :param state (RuntimeHarness): 精确快照测试身份
    :param reported (list[dict[str, object]]): 普通进程观察
    """
    response = await client.post(
        "/api/v1/node-api/reconcile",
        json={
            "node_id": str(state.node),
            "sections": ["runtime_sessions", "resources"],
            "snapshot": {"sessions": reported},
        },
    )
    assert response.status_code == 200


async def test_empty_reconciliation_before_launch_does_not_cancel_original_start(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    重现独立 Worker 对账先于启动的交错，原始就绪收据仍必须被接受。

    :param node_client (AsyncClient): 原节点认证客户端
    :param prepared (RuntimeHarness): 尚未启动的固定快照
    """
    ready = await start_result(prepared)
    await reconcile(node_client, prepared, [])
    response = await node_client.post(
        f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result", json=ready
    )
    assert response.status_code == 200
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "running"


@pytest.mark.parametrize("status", ["starting", "running", "active"])
@pytest.mark.parametrize("observation", ["missing", "inactive", "process_exited"])
async def test_generic_observation_preserves_managed_status_and_clean_termination(
    node_client: AsyncClient, prepared: RuntimeHarness, status: str, observation: str
) -> None:
    """
    通用缺失或退出不能抢先撤销受管状态、创建清理任务或污染干净终止。

    :param node_client (AsyncClient): 原节点认证客户端
    :param prepared (RuntimeHarness): 持久快照和原始任务
    :param status (str): 当前合法活跃阶段
    :param observation (str): 不具备原始调用权威的普通观察
    """
    await start_result(prepared)
    async with prepared.database.begin() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = status
        before = await session.scalar(select(func.count()).select_from(NodeTask))
    reported = (
        []
        if observation == "missing"
        else [
            {
                "session_id": str(prepared.session),
                "runtime_backend": "native",
                "active": False,
                "exit_reason": observation,
            }
        ]
    )
    await reconcile(node_client, prepared, reported)
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == status
        assert await session.scalar(select(func.count()).select_from(NodeTask)) == before
        assert await session.get(SkillSnapshotTermination, prepared.snapshot) is None
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination",
        json=await termination_input(prepared),
    )
    assert response.status_code == 200
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "stopped"


async def test_saved_snapshot_guards_reconciliation_without_mutable_task_marker(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    任务标记损坏不能让已有快照退回旧清单处理路径。

    :param node_client (AsyncClient): 原节点认证客户端
    :param prepared (RuntimeHarness): 已预约的原始快照
    """
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.payload = {key: value for key, value in task.payload.items() if key != "skill_manager"}
    await reconcile(node_client, prepared, [])
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert runtime is not None and runtime.status == "starting"
        assert snapshot is not None and snapshot.status == "reserved"
