"""
用真实 PostgreSQL 锁证明精确快照不能越过尚未提交的共享内容删除标记。
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.models.skill_storage import SkillContentObject
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.snapshots import SkillSnapshotService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.mark.parametrize("commit_marker", [False, True])
async def test_postgres_snapshot_waits_for_shared_deletion_marker(
    prepared: RuntimeHarness, tmp_path: Path, commit_marker: bool
) -> None:
    """
    在标记写入阶段注入删除状态，预约真实阻塞；标记提交后拒绝，回滚后才可原子建立引用。

    :param prepared (RuntimeHarness): 尚未预约的会话与真实数据库
    :param tmp_path (Path): 私有内容卷
    :param commit_marker (bool): 删除标记是否提交
    """
    await require_postgres(prepared.database)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    info = await library.info()
    revision = next(row for row in info.revisions if row.id == info.default_revision_id)
    async with prepared.database.begin() as session:
        content = content_service(session, tmp_path)
        manifest = await content.read_tree(prepared.owner, "package", revision.content_digest)
        upload = await content.begin(prepared.owner, str(uuid4()), manifest, "state")
        await content.complete(prepared.owner, upload.id)
    digest = manifest.entries[0].sha256
    ready = asyncio.Event()
    writer_pid: list[int] = []
    task: asyncio.Task[str] | None = None

    async def reserve_snapshot() -> str:
        """
        第二连接直接执行真实预约，业务异常不能留下部分分支或快照。

        :return str: 已提交或明确业务拒绝
        """
        async with prepared.database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            try:
                await SkillSnapshotService(
                    session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
                ).reserve(prepared.owner, prepared.session, prepared.task, {})
            except SkillContentError as error:
                return error.code
            return "committed"

    try:
        async with prepared.database() as marking:
            await SkillStorageRepository(marking).lock_usage(prepared.owner)
            marker = await marking.get(SkillContentObject, (prepared.owner, "state", digest))
            assert marker is not None
            marker.status = "deleting"
            await marking.flush()
            task = asyncio.create_task(reserve_snapshot())
            await asyncio.wait_for(ready.wait(), 3)
            blocked = False
            async with prepared.database() as observer:
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
            if commit_marker:
                await marking.commit()
            else:
                await marking.rollback()
    finally:
        if task is not None:
            outcome = await asyncio.wait_for(task, 5)
    assert outcome == ("CONTENT_UNAVAILABLE" if commit_marker else "committed")
    async with prepared.database() as session:
        snapshots = (
            await session.scalars(
                select(SessionSkillSnapshot).where(SessionSkillSnapshot.user_id == prepared.owner)
            )
        ).all()
        branch = await session.get(AccountSkillState, prepared.state)
        assert branch is not None
        assert len(snapshots) == (0 if commit_marker else 1)
        assert (branch.head_checkpoint_id is None) == commit_marker
        if snapshots:
            actual = await content_service(session, tmp_path).read_tree(
                prepared.owner, "state", snapshots[0].tree_digest
            )
            assert any(entry.sha256 == digest for entry in actual.entries)
    path = tmp_path / "objects" / str(prepared.owner) / digest[:2] / digest
    assert path.is_file()
