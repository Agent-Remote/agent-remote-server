"""
验证运行时对账的旧候选不能越过历史删除和后续生命周期。
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_legacy_session_result_races import legacy_runtime
from test_runtime_migration_recovery_races import wait_blocked
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, Session, User
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.services import sessions as session_services
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.sessions import ToolSessionService


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("bulk", [False, True])
async def test_stale_cleanup_candidate_waits_for_deletion_without_enqueuing(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: bool,
    bulk: bool,
) -> None:
    """
    对账先读取运行状态但删除先持有锁，删除提交后不能凭旧对象插入清理。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param tmp_path (Path): 夹具目录
    :param monkeypatch (pytest.MonkeyPatch): 原候选及删除栅栏
    :param history (bool): 是否存在用户历史互斥行
    :param bulk (bool): 是否使用批量删除
    """
    await require_postgres(database)
    prepared = await legacy_runtime(database, tmp_path)
    async with database.begin() as seed:
        runtime = await seed.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = "running"
        if not history:
            await seed.execute(
                delete(SkillStorageUsage).where(SkillStorageUsage.user_id == prepared.owner)
            )
    read, proceed, locked, release = (asyncio.Event() for _ in range(4))
    async with database() as reconciling, database() as deleting:
        node = await reconciling.get(Node, prepared.node)
        user = await deleting.get(User, prepared.owner)
        assert node is not None and user is not None
        service = NodeService(reconciling, Settings())
        original_list = service._repository.list_legacy_active_sessions_for_node
        original_check = session_services.require_settled_session_stops

        async def stale(node_id: UUID) -> Sequence[Session]:
            """
            保留确实从数据库读取的旧运行候选。

            :param node_id (UUID): 原节点
            :return Sequence[Session]: 旧运行会话对象
            """
            rows = await original_list(node_id)
            assert any(row.id == prepared.session for row in rows)
            read.set()
            await asyncio.wait_for(proceed.wait(), 10)
            return rows

        async def hold_delete(session: AsyncSession, runtime: Session) -> None:
            """
            完成真实预检后保持删除持有的数据库锁。

            :param session (AsyncSession): 删除连接
            :param runtime (Session): 原会话
            """
            await original_check(session, runtime)
            locked.set()
            await asyncio.wait_for(release.wait(), 10)

        async def remove() -> None:
            """
            运行真实单项或批量删除服务。
            """
            service = ToolSessionService(deleting, Settings())
            if bulk:
                assert await service.delete_inactive_sessions(user=user) == 1
            else:
                await service.delete_session(user=user, session_id=prepared.session)

        monkeypatch.setattr(service._repository, "list_legacy_active_sessions_for_node", stale)
        monkeypatch.setattr(session_services, "require_settled_session_stops", hold_delete)
        process = int(await reconciling.scalar(select(func.pg_backend_pid())) or 0)
        reconcile = asyncio.create_task(
            service.reconcile(
                node=node,
                node_id=node.id,
                sections=["runtime_sessions"],
                snapshot={
                    "sessions": [
                        {
                            "session_id": str(prepared.session),
                            "runtime_backend": "native",
                            "active": False,
                            "exit_reason": "process_exited",
                        }
                    ]
                },
            )
        )
        await asyncio.wait_for(read.wait(), 5)
        async with database.begin() as changed:
            runtime = await changed.get(Session, prepared.session)
            assert runtime is not None
            runtime.status = "stopped"
        removal = asyncio.create_task(remove())
        try:
            await asyncio.wait_for(locked.wait(), 5)
            proceed.set()
            await wait_blocked(database, process)
            release.set()
            await asyncio.wait_for(removal, 5)
            await asyncio.wait_for(reconcile, 5)
        finally:
            proceed.set()
            release.set()
            for task in (removal, reconcile):
                if not task.done():
                    task.cancel()
            await asyncio.gather(removal, reconcile, return_exceptions=True)
            await deleting.rollback()
            await reconciling.rollback()
    async with database() as observed:
        assert await observed.get(Session, prepared.session) is None
        assert (
            await observed.scalar(
                select(NodeTask).where(
                    NodeTask.task_id == f"cleanup_tool_session:{prepared.session}"
                )
            )
            is None
        )


@pytest.mark.parametrize("history", [False, True])
async def test_cleanup_insertion_blocks_delete_until_pending_task_is_visible(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: bool,
) -> None:
    """
    清理先持锁时，删除等待并观察新任务，不能使用先前运行状态或空任务查询。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param tmp_path (Path): 夹具目录
    :param monkeypatch (pytest.MonkeyPatch): 控制清理提交
    :param history (bool): 是否存在用户历史互斥行
    """
    await require_postgres(database)
    prepared = await legacy_runtime(database, tmp_path)
    async with database.begin() as seed:
        runtime = await seed.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = "running"
        if not history:
            await seed.execute(
                delete(SkillStorageUsage).where(SkillStorageUsage.user_id == prepared.owner)
            )
    entered, release = asyncio.Event(), asyncio.Event()
    async with database() as reconciling, database() as deleting:
        node = await reconciling.get(Node, prepared.node)
        user = await deleting.get(User, prepared.owner)
        assert node is not None and user is not None
        service = NodeService(reconciling, Settings())
        original = service._repository.add_task

        async def hold(task: NodeTask) -> NodeTask:
            """
            原清理任务已写入但尚未提交时阻塞事务。

            :param task (NodeTask): 准确原清理任务
            :return NodeTask: 已写入的任务
            """
            value = await original(task)
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            return value

        monkeypatch.setattr(service._repository, "add_task", hold)
        process = int(await deleting.scalar(select(func.pg_backend_pid())) or 0)
        reconcile = asyncio.create_task(
            service.reconcile(
                node=node,
                node_id=node.id,
                sections=["runtime_sessions"],
                snapshot={
                    "sessions": [
                        {
                            "session_id": str(prepared.session),
                            "runtime_backend": "native",
                            "active": False,
                            "exit_reason": "process_exited",
                        }
                    ]
                },
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        removal = asyncio.create_task(
            ToolSessionService(deleting, Settings()).delete_session(
                user=user, session_id=prepared.session
            )
        )
        try:
            await wait_blocked(database, process)
            release.set()
            await asyncio.wait_for(reconcile, 5)
            with pytest.raises(ApiError) as error:
                await asyncio.wait_for(removal, 5)
            assert error.value.code == "SESSION_STOP_UNRESOLVED"
        finally:
            release.set()
            for task in (removal, reconcile):
                if not task.done():
                    task.cancel()
            await asyncio.gather(removal, reconcile, return_exceptions=True)
            await deleting.rollback()
            await reconciling.rollback()
    async with database() as observed:
        runtime = await observed.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "interrupted"


@pytest.mark.parametrize("state", ["stopped", "stopping", "failed"])
async def test_stale_inventory_preserves_later_lifecycle(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """
    对账读到候选后生命周期已变化，锁内刷新不能再改为中断或创建清理任务。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param tmp_path (Path): 夹具目录
    :param monkeypatch (pytest.MonkeyPatch): 在候选读取与加锁间提交新状态
    :param state (str): 较新的会话生命周期
    """
    await require_postgres(database)
    prepared = await legacy_runtime(database, tmp_path)
    async with database.begin() as seed:
        runtime = await seed.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = "running"
    async with database() as reconciling:
        node = await reconciling.get(Node, prepared.node)
        assert node is not None
        service = NodeService(reconciling, Settings())
        original = service._repository.list_legacy_active_sessions_for_node

        async def stale(node_id: UUID) -> Sequence[Session]:
            """
            另一连接提交后仍返回已读取的旧 ORM 实例。

            :param node_id (UUID): 原节点
            :return Sequence[Session]: 旧运行态候选
            """
            rows = await original(node_id)
            assert len(rows) == 1 and rows[0].status == "running"
            async with database.begin() as changing:
                runtime = await changing.get(Session, prepared.session)
                assert runtime is not None
                runtime.status = state
            return rows

        monkeypatch.setattr(service._repository, "list_legacy_active_sessions_for_node", stale)
        await service.reconcile(
            node=node,
            node_id=node.id,
            sections=["runtime_sessions"],
            snapshot={
                "sessions": [
                    {
                        "session_id": str(prepared.session),
                        "runtime_backend": "native",
                        "active": False,
                        "exit_reason": "process_exited",
                    }
                ]
            },
        )
    async with database() as observed:
        runtime = await observed.get(Session, prepared.session)
        assert runtime is not None and runtime.status == state
        assert (
            await observed.scalar(
                select(NodeTask).where(
                    NodeTask.task_id == f"cleanup_tool_session:{prepared.session}"
                )
            )
            is None
        )
