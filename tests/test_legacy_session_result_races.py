"""
通过真实 PostgreSQL 锁等待验证旧会话任务并发结算只有一个原收据。
"""

import asyncio
from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_runtime_migration_recovery_races import wait_blocked
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database
from test_skill_snapshots import prepare_runtime

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, NodeTaskResult, Session
from agent_remote_server.services.nodes import NodeService


async def legacy_runtime(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> RuntimeHarness:
    """
    建立没有受管启动标记或快照的真实原始启动任务。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param tmp_path (Path): 内容夹具目录
    :return RuntimeHarness: 原任务与会话身份
    """
    prepared = await prepare_runtime(database, tmp_path)
    async with database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None and "skill_manager" not in task.payload
        task.status = "leased"
    return prepared


@pytest.mark.parametrize("conflicting", [False, True])
async def test_parallel_session_results_preserve_first_receipt(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflicting: bool,
) -> None:
    """
    第二连接明确等待首次结算，随后只能重放相同收据或拒绝冲突。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param tmp_path (Path): 内容夹具目录
    :param monkeypatch (pytest.MonkeyPatch): 控制首次提交时刻
    :param conflicting (bool): 并发回报是否故意冲突
    """
    await require_postgres(database)
    prepared = await legacy_runtime(database, tmp_path)
    result: dict[str, object] = {
        "status": "running",
        "session_id": str(prepared.session),
        "tmux_session_name": "original",
    }
    accepted, release = asyncio.Event(), asyncio.Event()
    async with database() as first, database() as second:
        node = await first.get(Node, prepared.node)
        other_node = await second.get(Node, prepared.node)
        assert node is not None and other_node is not None
        service = NodeService(first, Settings())
        original = service._apply_tool_session_task_result

        async def hold(task: NodeTask, value: dict[str, object]) -> None:
            """
            应用真实副作用后保持事务锁，等待观察者确认第二事务阻塞。

            :param task (NodeTask): 原始任务
            :param value (dict[str, object]): 首次真实结果
            """
            await original(task, value)
            accepted.set()
            await asyncio.wait_for(release.wait(), 10)

        monkeypatch.setattr(service, "_apply_tool_session_task_result", hold)
        process = int(await second.scalar(select(func.pg_backend_pid())) or 0)
        winner = asyncio.create_task(
            service.complete_task(node=node, task_id=str(prepared.task), result=result)
        )
        await asyncio.wait_for(accepted.wait(), 3)
        contender = asyncio.create_task(
            NodeService(second, Settings()).complete_task(
                node=other_node,
                task_id=str(prepared.task),
                result={**result, "tmux_session_name": "conflicting"} if conflicting else result,
            )
        )
        try:
            await wait_blocked(database, process)
            release.set()
            await asyncio.wait_for(winner, 3)
            if conflicting:
                with pytest.raises(ApiError) as error:
                    await asyncio.wait_for(contender, 3)
                assert error.value.code == "SESSION_TASK_RESULT_CONFLICT"
            else:
                await asyncio.wait_for(contender, 3)
        finally:
            release.set()
            for pending in (winner, contender):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(winner, contender, return_exceptions=True)
            await second.rollback()
    async with database() as observed:
        runtime = await observed.get(Session, prepared.session)
        assert runtime is not None and runtime.tmux_session_name == "original"
        receipts = (
            await observed.scalars(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == prepared.task)
            )
        ).all()
        assert len(receipts) == 1 and receipts[0].result == result
