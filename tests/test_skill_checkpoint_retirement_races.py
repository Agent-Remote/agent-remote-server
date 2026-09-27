"""
用真实 PostgreSQL 独立连接验证提前退役与重新 pin 的串行提交边界。
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_checkpoint_retirement import historical_selection
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command

from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.planner import SkillHistoryRetirementPlanner
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.mark.parametrize("retirement_first", [True, False])
@pytest.mark.parametrize("planned", [True, False])
async def test_postgres_checkpoint_retirement_serializes_with_pin(
    stopped: RuntimeHarness, tmp_path: Path, retirement_first: bool, planned: bool
) -> None:
    """
    pin 先提交阻止回收，退役先提交保留 expired；后到写入须等待锁并读取最新状态。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param retirement_first (bool): 是否先执行退役但尚未提交
    :param planned (bool): 是否通过完整预览计划执行退役
    """
    async with stopped.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    selected, head_id, revision_id = await historical_selection(stopped, tmp_path)
    async with stopped.database() as session:
        plan = await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).preview(
            stopped.owner, stopped.account, selected, all_unreferenced=True
        )

    async def retire_selected(session: AsyncSession) -> None:
        """
        比较原精确选择与完整计划执行的相同锁边界。

        :param session (AsyncSession): 独立写事务
        """
        if planned:
            await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).apply(
                stopped.owner, plan
            )
        else:
            await SkillHistoryRetirementService(session, SkillStoragePolicy()).retire(
                stopped.owner, stopped.account, selected, all_unreferenced=True
            )

    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    request = SkillRuleRequest(
        command="pin",
        skill="learning",
        revision=str(revision_id),
        scope=SkillScope(account_id=stopped.account),
        expected_generation=await library.generation(),
        idempotency_key=str(uuid4()),
    )
    ready = asyncio.Event()
    writer_pid: list[int] = []
    task: asyncio.Task[str] | None = None

    async def competing_writer() -> str:
        """
        在另一连接中提交相反操作，保留明确业务拒绝用于比较。

        :return str: 提交成功或真实业务错误码
        """
        async with stopped.database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            try:
                if retirement_first:
                    await library.service(session).execute(stopped.owner, request)
                else:
                    await retire_selected(session)
            except SkillContentError as error:
                return error.code
            return "committed"

    try:
        async with stopped.database() as session:
            if retirement_first:
                await retire_selected(session)
            else:
                await library.service(session).execute(stopped.owner, request)
            task = asyncio.create_task(competing_writer())
            await asyncio.wait_for(ready.wait(), timeout=2)
            blocked = False
            async with stopped.database() as observer:
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
            await session.commit()
    finally:
        if task is not None:
            result = await asyncio.wait_for(task, timeout=5)
    rejection = "HEAD_CHANGED" if planned else "STATE_PROTECTED"
    assert result == ("committed" if retirement_first else rejection)
    current = (await command(stopped, tmp_path)).expected.targets[0]
    assert current.revision_id == revision_id and current.expired == retirement_first
    async with stopped.database() as session:
        head = await session.get(SkillCheckpoint, head_id)
        assert head is not None and head.retained != retirement_first
