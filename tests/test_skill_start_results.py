"""
验证受管启动完成回报绑定原始领取轮次，历史重放不复活会话。
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_history_retirement import retire
from test_skill_snapshot_lease import prepare_attempt
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask, NodeTaskResult, Session, User
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.skill_manager.retention.graph import RetentionKey


async def test_failed_lifecycle_mutation_rolls_back_start_acceptance(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    启动结果后续生命周期步骤失败时，即使外层提交也不留下阶段、任务或结果的部分更新。

    :param prepared (RuntimeHarness): 原始预约身份
    :param tmp_path (Path): 测试内容卷
    :param monkeypatch (pytest.MonkeyPatch): 生命周期故障注入
    """
    from test_skill_snapshots import reserve

    from agent_remote_server.config import Settings
    from agent_remote_server.models import Node
    from agent_remote_server.services.nodes import NodeService

    snapshot = await reserve(prepared, tmp_path)
    prepared.snapshot = snapshot.id
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.status = "leased"
    result = await start_result(prepared)

    async def reject(task: NodeTask, result: dict[str, object]) -> None:
        """
        模拟后续生命周期事务失败。

        :param task (NodeTask): 已写入结果的原始任务
        :param result (dict[str, object]): 有界启动结果
        """
        raise RuntimeError("injected lifecycle failure")

    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        service = NodeService(session, Settings())
        monkeypatch.setattr(service, "_apply_tool_session_task_result", reject)
        with pytest.raises(RuntimeError, match="injected lifecycle failure"):
            await service.complete_task(node=node, task_id=str(prepared.task), result=result)
    async with prepared.database() as session:
        saved_snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        runtime = await session.get(Session, prepared.session)
        task = await session.get(NodeTask, prepared.task)
        assert saved_snapshot is not None and saved_snapshot.status == "reserved"
        assert runtime is not None and runtime.status == "starting"
        assert task is not None and task.status == "leased"
        assert (
            await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == prepared.task)
            )
            is None
        )


@pytest.mark.parametrize("stopped", [False, True])
async def test_confirmation_replay_preserves_retired_snapshot(
    node_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path, stopped: bool
) -> None:
    """
    真实收尾和退役之后仍可查询原始完成收据，不能恢复内容引用或改写历史时钟。

    :param node_client (AsyncClient): 原始节点的认证客户端
    :param prepared (RuntimeHarness): 原始预约及内容身份
    :param tmp_path (Path): 测试内容卷
    :param stopped (bool): 是否原本确认启动停止
    """
    result = await start_result(prepared, stopped)
    path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    accepted = await node_client.post(path, json=result)
    assert accepted.status_code == 200, accepted.text
    async with prepared.database.begin() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = "stopped"
    finalization_id = await ingest(prepared, tmp_path, {})
    publication = await publish(prepared, tmp_path, finalization_id)
    await retire(
        prepared,
        RetentionKey("publication", str(publication.id)),
        RetentionKey("finalization", str(finalization_id)),
        RetentionKey("snapshot", str(prepared.snapshot)),
        early=True,
    )
    async with prepared.database() as session:
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert snapshot is not None and snapshot.content_retired_at is not None
        release = snapshot.retention_released_at
        retired = snapshot.content_retired_at
        assert release is not None
    replay = await node_client.post(path, json=result)
    assert replay.status_code == 200 and replay.json()["data"] == result, replay.text
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert runtime is not None and runtime.status == "stopped"
        assert snapshot is not None and snapshot.status == "retained"
        assert snapshot.retention_released_at == release
        assert snapshot.content_retired_at == retired


@pytest.mark.parametrize("stopped", [False, True])
async def test_managed_start_confirmation_echoes_exact_committed_receipt(
    node_client: AsyncClient, prepared: RuntimeHarness, stopped: bool
) -> None:
    """
    专用确认只回显已提交的原始有界结果，不要求重放时租约仍存活。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 原始预约
    :param stopped (bool): 是否确认停止
    """
    result = await start_result(prepared, stopped)
    path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    for _ in range(2):
        response = await node_client.post(path, json=result)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed" and body["committed"] is True
        assert body["data"] == result
    changed = await node_client.post(path, json=result | {"lease_attempt": 4})
    assert changed.status_code >= 400


async def test_managed_start_confirmation_never_upgrades_a_legacy_task(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    专用入口拒绝缺失受管指针，不能以回显模型冒充合法完成。

    :param node_client (AsyncClient): 认证客户端
    :param prepared (RuntimeHarness): 原始身份
    """
    result = await start_result(prepared)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.payload = {key: value for key, value in task.payload.items() if key != "skill_manager"}
    response = await node_client.post(
        f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result", json=result
    )
    assert response.status_code >= 400


@pytest.mark.parametrize("opposite", [False, True])
async def test_managed_start_result_serializes_concurrent_publication(
    node_client: AsyncClient, prepared: RuntimeHarness, opposite: bool
) -> None:
    """
    PostgreSQL 独立请求并发发布只保留一次原始结果，冲突结果不能覆盖。

    :param node_client (AsyncClient): 并发认证客户端
    :param prepared (RuntimeHarness): 原始身份
    :param opposite (bool): 是否提交冲突的停止结果
    """
    async with prepared.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    result = await start_result(prepared)
    stopped = {
        key: value
        for key, value in result.items()
        if key not in {"status", "tool_type", "tmux_session_name"}
    } | {
        "code": "SKILL_START_STOPPED",
        "message": "Managed startup stopped; skill finalization is pending.",
    }
    prefix = f"/api/v1/node-api/tasks/{prepared.task}/"
    responses = await asyncio.gather(
        node_client.post(prefix + "complete", json={"result": result}),
        node_client.post(
            prefix + ("fail" if opposite else "complete"),
            json={"error": stopped} if opposite else {"result": result},
        ),
    )
    assert sum(response.status_code == 200 for response in responses) == (1 if opposite else 2)
    async with prepared.database() as session:
        rows = list(
            (
                await session.scalars(
                    select(NodeTaskResult).where(NodeTaskResult.node_task_id == prepared.task)
                )
            ).all()
        )
        task = await session.get(NodeTask, prepared.task)
        runtime = await session.get(Session, prepared.session)
        assert len(rows) == 1 and task is not None and runtime is not None
        assert rows[0].status == task.status
        assert runtime.status == ("running" if task.status == "succeeded" else "failed")


async def start_result(prepared: RuntimeHarness, stopped: bool = False) -> dict[str, object]:
    """
    生成只含原始身份和中立运行信息的精确回报。

    :param prepared (RuntimeHarness): 原始快照与任务
    :param stopped (bool): 是否回报启动停止
    :return dict[str, object]: 有界完成或失败载荷
    """
    await prepare_attempt(prepared)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.payload = dict(task.payload) | {
            "tmux_session_name": "managed-test",
            "tool_type": "claude",
        }
    result: dict[str, object] = {
        "session_id": str(prepared.session),
        "tool_account_id": str(prepared.account),
        "runtime_backend": "native",
        "runtime_resource_id": "agent-remote-session-"
        + hashlib.sha256(str(prepared.session).encode()).hexdigest()[:12]
        + ".service",
        "skill_snapshot_id": str(prepared.snapshot),
        "task_record_id": str(prepared.task),
        "lease_attempt": 3,
    }
    if stopped:
        return result | {
            "code": "SKILL_START_STOPPED",
            "message": "Managed startup stopped; skill finalization is pending.",
        }
    return result | {
        "status": "running",
        "tool_type": "claude",
        "tmux_session_name": "managed-test",
    }


@pytest.mark.parametrize("stopped", [False, True])
async def test_managed_start_result_replay_never_revives_session(
    node_client: AsyncClient, prepared: RuntimeHarness, stopped: bool
) -> None:
    """
    精确完成只登记一次，收据重放不能把后来的停止状态改回运行。

    :param node_client (AsyncClient): 真实认证节点客户端
    :param prepared (RuntimeHarness): 已预约运行身份
    :param stopped (bool): 完成结果类型
    """
    result = await start_result(prepared, stopped)
    route, field = ("fail", "error") if stopped else ("complete", "result")
    path = f"/api/v1/node-api/tasks/{prepared.task}/{route}"
    response = await node_client.post(path, json={field: result})
    assert response.status_code == 200, response.text
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        runtime = await session.get(Session, prepared.session)
        assert task is not None and runtime is not None
        assert task.status == ("failed" if stopped else "succeeded")
        assert runtime.status == ("failed" if stopped else "running")
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert snapshot is not None and snapshot.status == ("finalizing" if stopped else "started")
        runtime.status = "stopped"
        task.lease_until = datetime.now(UTC) - timedelta(days=1)
    replay = await node_client.post(path, json={field: result})
    assert replay.status_code == 200, replay.text
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "stopped"
        count = await session.scalar(
            select(func.count())
            .select_from(NodeTaskResult)
            .where(NodeTaskResult.node_task_id == prepared.task)
        )
        assert count == 1
    changed = result | {"lease_attempt": 4}
    denied = await node_client.post(path, json={field: changed})
    assert denied.status_code >= 400


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "expired",
        "cancelled",
        "pending",
        "session",
        "owner",
        "snapshot",
        "task",
        "account",
        "unit",
        "tmux",
        "extra",
        "boolean_attempt",
        "marker",
        "missing_marker",
        "task_type",
    ],
)
async def test_managed_start_result_rejects_changed_authority(
    node_client: AsyncClient, prepared: RuntimeHarness, change: str
) -> None:
    """
    迟到或变更身份的完成不生成收据，也不改变原任务状态。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 固定原始身份
    :param change (str): 失效条件
    """
    result = await start_result(prepared)
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        runtime = await session.get(Session, prepared.session)
        owner = await session.get(User, prepared.owner)
        assert task is not None and runtime is not None and owner is not None
        if change == "attempt":
            task.retry_count = 4
        elif change == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif change in {"cancelled", "pending"}:
            task.status = change
        elif change == "session":
            runtime.status = "stopping"
        elif change == "owner":
            owner.status = "disabled"
        elif change == "marker":
            task.payload = dict(task.payload) | {"skill_manager": None}
        elif change == "missing_marker":
            task.payload = {
                key: value for key, value in task.payload.items() if key != "skill_manager"
            }
        elif change == "task_type":
            task.task_type = "prepare_workspace"
        else:
            fields = {
                "snapshot": "skill_snapshot_id",
                "task": "task_record_id",
                "account": "tool_account_id",
                "unit": "runtime_resource_id",
                "tmux": "tmux_session_name",
                "extra": "private",
                "boolean_attempt": "lease_attempt",
            }
            result[fields[change]] = True if change == "boolean_attempt" else str(uuid4())
        expected_status = task.status
    response = await node_client.post(
        f"/api/v1/node-api/tasks/{prepared.task}/complete", json={"result": result}
    )
    assert response.status_code >= 400, response.text
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None and task.status == expected_status
        assert (
            await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == prepared.task)
            )
            is None
        )


async def test_managed_start_failure_cannot_bypass_exact_outcome(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    通用失败回报也必须证明原始停止结果，不能将模糊启动缓存为终态。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 原始任务
    """
    result = await start_result(prepared)
    response = await node_client.post(
        f"/api/v1/node-api/tasks/{prepared.task}/fail",
        json={"error": {"code": "NODE_TASK_FAILED", "message": "private failure"}},
    )
    assert response.status_code >= 400
    accepted = await node_client.post(
        f"/api/v1/node-api/tasks/{prepared.task}/complete", json={"result": result}
    )
    assert accepted.status_code == 200, accepted.text
    conflict = await node_client.post(
        f"/api/v1/node-api/tasks/{prepared.task}/fail",
        json={
            "error": {
                key: value
                for key, value in result.items()
                if key not in {"status", "tool_type", "tmux_session_name"}
            }
            | {
                "code": "SKILL_START_STOPPED",
                "message": "Managed startup stopped; skill finalization is pending.",
            }
        },
    )
    assert conflict.status_code >= 400
