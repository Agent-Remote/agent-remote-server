"""
使用独立 PostgreSQL 连接证明停止与历史删除按原会话串行。
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_legacy_session_result_races import legacy_runtime
from test_runtime_migration_recovery_races import wait_blocked
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask, Session, User
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.services import sessions as session_services
from agent_remote_server.services.sessions import ToolSessionService


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("delete_first", [False, True])
async def test_stop_and_delete_serialize_original_session(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history: bool,
    bulk: bool,
    delete_first: bool,
) -> None:
    """
    等待真实数据库锁后刷新会话，已删除不能派停止任务，停止中的会话不能被旧读取删除。

    :param database (async_sessionmaker[AsyncSession]): 独立数据库连接
    :param tmp_path (Path): 夹具目录
    :param monkeypatch (pytest.MonkeyPatch): 精确事务交错栅栏
    :param history (bool): 是否存在用户历史互斥行
    :param bulk (bool): 是否使用整批删除
    :param delete_first (bool): 是否删除先持有锁
    """
    await require_postgres(database)
    prepared = await legacy_runtime(database, tmp_path)
    async with database.begin() as seed:
        runtime = await seed.get(Session, prepared.session)
        assert runtime is not None
        runtime.status = "interrupted"
        if not history:
            await seed.execute(
                delete(SkillStorageUsage).where(SkillStorageUsage.user_id == prepared.owner)
            )
    entered, release = asyncio.Event(), asyncio.Event()
    async with database() as stopping, database() as deleting:
        stop_user = await stopping.get(User, prepared.owner)
        delete_user = await deleting.get(User, prepared.owner)
        assert stop_user is not None and delete_user is not None
        # 预读故意污染 ORM 身份映射，验证锁后的 populate_existing 确实刷新。
        assert await stopping.get(Session, prepared.session) is not None
        assert await deleting.get(Session, prepared.session) is not None
        stop_service = ToolSessionService(stopping, Settings())
        delete_service = ToolSessionService(deleting, Settings())
        original_add = stop_service._repository.add_task
        original_check = session_services.require_settled_session_stops

        async def hold_stop(task: NodeTask) -> NodeTask:
            """
            原停止任务落库后保持会话锁，尚不提交。

            :param task (NodeTask): 新建的原停止任务
            :return NodeTask: 已持久化于当前事务的任务
            """
            saved = await original_add(task)
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
            return saved

        async def hold_delete(session: AsyncSession, runtime: Session) -> None:
            """
            全部原停止检查通过后保持删除事务锁。

            :param session (AsyncSession): 删除连接
            :param runtime (Session): 原会话
            """
            await original_check(session, runtime)
            entered.set()
            await asyncio.wait_for(release.wait(), 10)

        async def remove() -> int | None:
            """
            通过实际服务运行单项或批量删除。

            :return int | None: 整批删除数量或单项无返回
            """
            if bulk:
                return await delete_service.delete_inactive_sessions(user=delete_user)
            await delete_service.delete_session(user=delete_user, session_id=prepared.session)
            return None

        async def stop() -> Session:
            """
            通过实际服务受理准确原会话停止。

            :return Session: 停止后的原会话
            """
            return await stop_service.stop_session(user=stop_user, session_id=prepared.session)

        if delete_first:
            monkeypatch.setattr(session_services, "require_settled_session_stops", hold_delete)
            contender_connection = stopping
        else:
            monkeypatch.setattr(stop_service._repository, "add_task", hold_stop)
            contender_connection = deleting
        process = int(await contender_connection.scalar(select(func.pg_backend_pid())) or 0)
        winner = asyncio.create_task(remove() if delete_first else stop())
        await asyncio.wait_for(entered.wait(), 5)
        contender = asyncio.create_task(stop() if delete_first else remove())
        try:
            await wait_blocked(database, process)
            release.set()
            await asyncio.wait_for(winner, 5)
            if not delete_first and bulk:
                assert await asyncio.wait_for(contender, 5) == 0
            else:
                with pytest.raises(ApiError) as error:
                    await asyncio.wait_for(contender, 5)
                assert error.value.code == (
                    "COMMON_NOT_FOUND" if delete_first else "SESSION_DELETE_NOT_ALLOWED"
                )
        finally:
            release.set()
            for task in (winner, contender):
                if not task.done():
                    task.cancel()
            await asyncio.gather(winner, contender, return_exceptions=True)
            await stopping.rollback()
            await deleting.rollback()
    async with database() as observed:
        runtime = await observed.get(Session, prepared.session)
        tasks = (
            await observed.scalars(
                select(NodeTask).where(NodeTask.task_id == f"stop_tool_session:{prepared.session}")
            )
        ).all()
        if delete_first:
            assert runtime is None and not tasks
        else:
            assert runtime is not None and runtime.status == "stopping" and len(tasks) == 1
