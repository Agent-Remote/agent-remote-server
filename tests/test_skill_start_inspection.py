"""
验证丢失启动确认后的只读收据观察和旧轮次提交隔离。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_snapshots import prepared as prepared
from test_skill_start_results import start_result

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, NodeTask, NodeTaskResult, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.nodes import NodeService


async def test_new_attempt_observation_fences_already_waiting_confirmation(
    node_client: AsyncClient, prepared: RuntimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    PostgreSQL 中旧确认已到达但等待锁时，新轮次提交后的缺席观察仍能排除迟到接受。

    :param node_client (AsyncClient): 使用独立数据库事务的认证客户端
    :param prepared (RuntimeHarness): 原始预约及领取轮次
    :param monkeypatch (pytest.MonkeyPatch): 精确观察请求到达授权锁前的位置
    """
    async with prepared.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    outcome = await start_result(prepared)
    original = NodeService._require_node_task
    waiting = asyncio.Event()
    entered: set[int] = set()

    async def require_task(service: NodeService, *, node: Node, task_id: str) -> NodeTask:
        """
        两个独立请求进入任务授权路径后才释放新轮次事务。

        :param service (NodeService): 请求自己的节点服务
        :param node (Node): 认证节点
        :param task_id (str): 原始逻辑任务身份
        :return NodeTask: 原始任务实体
        """
        current = asyncio.current_task()
        assert current is not None
        entered.add(id(current))
        if len(entered) == 2:
            waiting.set()
        return await original(service, node=node, task_id=task_id)

    monkeypatch.setattr(NodeService, "_require_node_task", require_task)
    path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    requests = []
    try:
        async with prepared.database.begin() as session:
            await SkillStorageRepository(session).lock_existing_usage(prepared.owner)
            task = await session.get(NodeTask, prepared.task)
            assert task is not None
            task.retry_count = 4
            task.lease_until = datetime.now(UTC) + timedelta(minutes=1)
            await session.flush()
            requests = [
                asyncio.create_task(node_client.post(path, json=outcome)),
                asyncio.create_task(node_client.post(path + "/inspect", json=outcome)),
            ]
            await asyncio.wait_for(waiting.wait(), timeout=5)
        confirmation, observation = await asyncio.gather(*requests)
        assert confirmation.status_code >= 400
        assert observation.status_code == 200, observation.text
        assert observation.json()["data"]["accepted"] is False
        assert observation.json()["data"]["current_lease_attempt"] == 4
    finally:
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.parametrize("stopped", [False, True])
async def test_start_inspection_observes_without_mutating(
    node_client: AsyncClient, prepared: RuntimeHarness, stopped: bool
) -> None:
    """
    查询未提交结果不续期或写入，提交后的查询只返回精确原始确认。

    :param node_client (AsyncClient): 认证节点客户端
    :param prepared (RuntimeHarness): 固定原始任务身份
    :param stopped (bool): 是否报告启动已停止
    """
    outcome = await start_result(prepared, stopped)
    path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        deadline = task.lease_until
    for _ in range(2):
        response = await node_client.post(path + "/inspect", json=outcome)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["committed"] is False and body["status"] == "unconfirmed"
        assert body["data"] == {
            "result": outcome,
            "accepted": False,
            "current_lease_attempt": 3,
            "task_status": "leased",
        }
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        runtime = await session.get(Session, prepared.session)
        assert task is not None and task.lease_until == deadline and task.retry_count == 3
        assert snapshot is not None and snapshot.status == "reserved"
        assert runtime is not None and runtime.status == "starting"
        assert (
            await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == prepared.task)
            )
            is None
        )
    committed = await node_client.post(path, json=outcome)
    assert committed.status_code == 200, committed.text
    async with prepared.database.begin() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = "stopped"
    observed = await node_client.post(path + "/inspect", json=outcome)
    assert observed.status_code == 200, observed.text
    body = observed.json()
    assert body["committed"] is True and body["status"] == "completed"
    assert body["data"]["accepted"] is True and body["data"]["result"] == outcome
    assert body["data"]["task_status"] == ("failed" if stopped else "succeeded")
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "stopped"


async def test_new_poll_attempt_fences_old_unconfirmed_result(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    真实重新领取后的缺席观察禁止旧轮次迟到提交，新轮次仍须独立确认原运行结果。

    :param node_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原始任务和快照
    """
    original = await start_result(prepared)
    path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    async with prepared.database() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        tasks = await NodeService(session, Settings()).poll_tasks(node=node)
        assert len(tasks) == 1 and tasks[0].id == prepared.task and tasks[0].retry_count == 4
    observed = await node_client.post(path + "/inspect", json=original)
    assert observed.status_code == 200, observed.text
    assert observed.json()["data"]["current_lease_attempt"] == 4
    assert observed.json()["data"]["accepted"] is False
    late = await node_client.post(path, json=original)
    assert late.status_code >= 400
    newer = original | {"lease_attempt": 4}
    accepted = await node_client.post(path, json=newer)
    assert accepted.status_code == 200, accepted.text
    assert (await node_client.post(path + "/inspect", json=original)).status_code >= 400


@pytest.mark.parametrize("change", ["attempt", "snapshot", "record", "unit", "tmux", "marker"])
async def test_start_inspection_rejects_foreign_or_changed_receipt(
    node_client: AsyncClient, prepared: RuntimeHarness, change: str
) -> None:
    """
    不能将另一候选结果或变更授权解释为原始收据缺席。

    :param node_client (AsyncClient): 认证节点客户端
    :param prepared (RuntimeHarness): 原始预约
    :param change (str): 被篡改的身份或授权字段
    """
    original = await start_result(prepared)
    path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    assert (await node_client.post(path, json=original)).status_code == 200
    changed = dict(original)
    field = {
        "attempt": "lease_attempt",
        "snapshot": "skill_snapshot_id",
        "record": "task_record_id",
        "unit": "runtime_resource_id",
        "tmux": "tmux_session_name",
    }
    if change == "marker":
        async with prepared.database.begin() as session:
            task = await session.get(NodeTask, prepared.task)
            assert task is not None
            task.payload = {
                key: value for key, value in task.payload.items() if key != "skill_manager"
            }
    else:
        changed[field[change]] = 4 if change == "attempt" else str(uuid4())
    denied = await node_client.post(path + "/inspect", json=changed)
    assert denied.status_code >= 400
