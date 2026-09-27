"""
使用真实 PostgreSQL 写锁证明账户候选执行不会越过未提交的 pin 或上传。
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_prune_candidates import candidate_plan
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_storage import file_entry

from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.prune import SkillPruneService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.mark.parametrize("commit_pin", [True, False])
@pytest.mark.parametrize("change", ["pin", "upload"])
async def test_postgres_prune_waits_and_revalidates_whole_candidate_plan(
    stopped: RuntimeHarness, tmp_path: Path, commit_pin: bool, change: str
) -> None:
    """
    独立整理连接确实等待未提交 pin；提交后拒绝原选择，回滚后才允许发布原计划。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param commit_pin (bool): 首个 pin 事务是否提交
    :param change (str): 先持锁的 pin 或上传变更
    """
    async with stopped.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    old, _ = await shared_directory(stopped, tmp_path)
    predicted = await candidate_plan(stopped, tmp_path)
    assert predicted.compaction is not None
    plan = predicted.compaction
    async with stopped.database() as session:
        checkpoint = await session.get(SkillCheckpoint, old)
        assert checkpoint is not None
        branch = await session.get(AccountSkillState, checkpoint.state_id)
        assert branch is not None and branch.base_revision_id is not None
        revision = branch.base_revision_id
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    request = SkillRuleRequest(
        command="pin",
        skill="learning",
        revision=str(revision),
        scope=SkillScope(account_id=stopped.account),
        expected_generation=await library.generation(),
        idempotency_key=str(uuid4()),
    )
    ready = asyncio.Event()
    writer_pid: list[int] = []
    task: asyncio.Task[str] | None = None

    async def competing_compaction() -> str:
        """
        另一连接只使用既有预览，不重新选择历史内容。

        :return str: 实际提交或业务拒绝代码
        """
        async with stopped.database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            try:
                service = SkillPruneService(
                    session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
                )
                await service.apply(stopped.owner, predicted)
            except SkillContentError as error:
                return error.code
            return "committed"

    try:
        async with stopped.database() as session:
            if change == "pin":
                await library.service(session).execute(stopped.owner, request)
            else:
                pending = await content_service(session, tmp_path).begin(
                    stopped.owner,
                    str(uuid4()),
                    SkillTreeManifest(entries=(file_entry(b"historical", path="memory"),)),
                    "state",
                )
                assert pending.reserved_bytes == 0
            task = asyncio.create_task(competing_compaction())
            await asyncio.wait_for(ready.wait(), timeout=2)
            blocked = False
            async with stopped.database() as observer:
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
            if commit_pin:
                await session.commit()
            else:
                await session.rollback()
    finally:
        if task is not None:
            outcome = await asyncio.wait_for(task, timeout=5)
    rejected = "HEAD_CHANGED"
    assert outcome == (rejected if commit_pin else "committed")
    current = (await command(stopped, tmp_path)).expected
    assert (current.directory_head_id == plan.directory_head_id) == commit_pin
