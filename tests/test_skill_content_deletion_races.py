"""
用独立 PostgreSQL 连接证明共享删除标记与另一分类的新上传使用同一用户锁。
"""

import asyncio
import io
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_storage import file_entry

from agent_remote_server.models.skill_storage import SkillContentObject
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError


@pytest.mark.parametrize("commit_marker", [False, True])
async def test_postgres_cross_category_admission_waits_for_deletion_marker(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, commit_marker: bool
) -> None:
    """
    新状态上传等待包分类标记事务，提交后拒绝，回滚后才允许完整登记同一文件。

    :param database (async_sessionmaker[AsyncSession]): 真实独立连接工厂
    :param tmp_path (Path): 私有内容卷
    :param commit_marker (bool): 是否提交第一份删除标记事务
    """
    async with database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    owner = await user(database)
    data = b"cross-category deletion race"
    entry = file_entry(data)
    manifest = SkillTreeManifest(entries=(entry,))
    async with database.begin() as session:
        svc = service(session, tmp_path)
        upload = await svc.begin(owner, str(uuid4()), manifest, "package")
        await svc.put_file(owner, upload.id, entry.sha256, io.BytesIO(data))
        await svc.complete(owner, upload.id)
    ready = asyncio.Event()
    writer_pid: list[int] = []
    task: asyncio.Task[str] | None = None

    async def admit_state() -> str:
        """
        第二连接实际执行新分类的 begin 与 complete，不使用进程内锁模拟等待。

        :return str: 真实成功或业务拒绝码
        """
        async with database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            svc = service(session, tmp_path)
            try:
                upload = await svc.begin(owner, str(uuid4()), manifest, "state")
                await svc.complete(owner, upload.id)
            except SkillContentError as error:
                return error.code
            return "committed"

    try:
        async with database() as session:
            await SkillStorageRepository(session).lock_usage(owner)
            marker = await session.get(SkillContentObject, (owner, "package", entry.sha256))
            assert marker is not None
            marker.status = "deleting"
            await session.flush()
            task = asyncio.create_task(admit_state())
            await asyncio.wait_for(ready.wait(), timeout=2)
            blocked = False
            async with database() as observer:
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
            if commit_marker:
                await session.commit()
            else:
                await session.rollback()
    finally:
        if task is not None:
            outcome = await asyncio.wait_for(task, timeout=5)
    assert outcome == ("CONTENT_UNAVAILABLE" if commit_marker else "committed")
    async with database() as session:
        state = await session.get(SkillContentObject, (owner, "state", entry.sha256))
        assert (state is None) == commit_marker
