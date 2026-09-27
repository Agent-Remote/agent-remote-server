"""
用独立 PostgreSQL 连接证明完整磁盘核验及取消清理始终受同一个内容用户锁保护。
"""

import asyncio
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database
from test_skill_finalization import service
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_reclamation import SkillReclamationAuthorization
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@pytest.mark.parametrize("cancel_read", [False, True])
async def test_postgres_reclamation_holds_gc_lock_until_physical_verification_exits(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_read: bool
) -> None:
    """
    等待实际磁盘线程时，参考写入或回收所用的用户锁不能提前释放，即使请求被取消。

    :param stopped (RuntimeHarness): 原始终态身份及独立数据库
    :param tmp_path (Path): 私有内容卷
    :param monkeypatch (pytest.MonkeyPatch): 仅在磁盘线程边界插入可控屏障
    :param cancel_read (bool): 是否在完整核验中取消请求
    """
    await require_postgres(stopped.database)
    receipt_id = await ingest(stopped, tmp_path, {"learning/state.bin": b"retained"})
    await publish(stopped, tmp_path, receipt_id)
    entered, release = threading.Event(), threading.Event()
    original = PrivateObjectStore._verify_manifest

    def paused_verify(
        store: PrivateObjectStore, owner_id: UUID, manifest: SkillTreeManifest
    ) -> None:
        """
        在真实对象校验之前阻塞线程，解除后仍执行全部实际字节检查。

        :param store (PrivateObjectStore): 私有对象卷
        :param owner_id (UUID): 已授权所有者
        :param manifest (SkillTreeManifest): 实际完整清单
        """
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test verification barrier expired")
        original(store, owner_id, manifest)

    monkeypatch.setattr(PrivateObjectStore, "_verify_manifest", paused_verify)

    async def authorize() -> SkillReclamationAuthorization:
        """
        在首个独立事务内执行真实回收核验。

        :return SkillReclamationAuthorization: 已完成的短期核验
        """
        async with stopped.database.begin() as session:
            return await service(session, tmp_path).authorize_reclamation(
                stopped.node, receipt_id, uuid4()
            )

    ready = asyncio.Event()
    writer_pid: list[int] = []

    async def competing_content_lock() -> None:
        """
        第二连接请求与内容 GC 和新引用提交相同的排他锁，不模拟物理删除。
        """
        async with stopped.database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            await SkillStorageRepository(session).lock_usage(stopped.owner)

    checking = asyncio.create_task(authorize())
    contender: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        contender = asyncio.create_task(competing_content_lock())
        await asyncio.wait_for(ready.wait(), 3)
        async with stopped.database() as observer:
            blocked = False
            for _ in range(100):
                if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                    blocked = True
                    break
                await asyncio.sleep(0.01)
            assert blocked and not contender.done()
            if cancel_read:
                checking.cancel()
                await asyncio.sleep(0)
                assert not checking.done()
                assert await observer.scalar(select(func.pg_blocking_pids(writer_pid[0])))
    finally:
        release.set()
        if cancel_read:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(checking, 5)
        else:
            authorization = await asyncio.wait_for(checking, 5)
            assert authorization.finalization_id == receipt_id
        if contender is not None:
            await asyncio.wait_for(contender, 5)
