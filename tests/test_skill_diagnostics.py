"""
验证诊断读不写入、真实等待分类、用户隔离和实际删除后的独立计量。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_compaction_reclamation import persisted_content
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_gc_worker import make_due, pending_deletion
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_content_service import user
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_prune_commands import full_preview, service
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.api.deps import get_settings
from agent_remote_server.config import Settings
from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_prune_operations import SkillPruneOperationDeletion
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_prune import PruneCommand
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.diagnostics import SkillDiagnosticService
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_empty_user_and_custom_policy_are_read_only(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """
    新用户查询返回零而不插入行，实际配置不是固定默认额度。

    :param database (async_sessionmaker[AsyncSession]): 独立数据库工厂
    """
    owner = await user(database)
    policy = SkillStoragePolicy(user_package_bytes=123, history_days=7, archive_days=19)
    async with database.begin() as session:
        view = await SkillDiagnosticService(session, policy).storage_view(owner)
        assert view.package_bytes == view.state_bytes == view.package_reserved_bytes == 0
        assert view.policy == policy and view.node_storage == "not_observed"
        assert view.deletion.pending_tasks == view.deletion.completed_tasks == 0
        assert not session.new and not session.dirty and not session.deleted
    async with database() as session:
        assert await session.get(SkillStorageUsage, owner) is None


async def test_history_diagnostic_uses_real_release_and_protection(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    到期、等待和未知严格来自原时钟，受保护当前项永远没有有效截止。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 私有内容卷
    """
    old, current = await shared_directory(stopped, tmp_path)
    policy = SkillStoragePolicy(history_days=7)
    for days, expected in [(None, "release_unknown"), (2, "waiting"), (8, "due")]:
        async with stopped.database.begin() as session:
            row = await session.get(SkillCheckpoint, old)
            assert row is not None
            row.retention_released_at = datetime.now(UTC) - timedelta(days=days) if days else None
        before = await persisted_content(stopped)
        async with stopped.database.begin() as session:
            diagnostic = SkillDiagnosticService(session, policy)
            views = await diagnostic.histories(stopped.owner, "checkpoint", (old, current))
            assert views[old].state == expected
            assert (views[old].expires_at is None) == (days is None)
            assert views[current].state == "protected" and views[current].protected_by
            assert views[current].expires_at is None
            assert not session.new and not session.dirty and not session.deleted
        assert await persisted_content(stopped) == before
    foreign = await user(stopped.database)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError, match="history not found"):
            await SkillDiagnosticService(session, policy).histories(foreign, "checkpoint", (old,))


async def test_http_info_and_storage_follow_actual_cleanup_without_rewriting_receipt(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    真实清理前后可观察逻辑与磁盘阶段，详情授权身份不变且查询不写时钟。

    :param user_client (AsyncClient): 真实认证客户端
    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 私有内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    before = await persisted_content(stopped)
    info = await user_client.get("/api/v1/skills/installations/learning")
    assert info.status_code == 200, info.text
    data = info.json()["data"]
    assert data["storage"]["scope"] == "user"
    assert all(row["retention"]["id"] == row["id"] for row in data["revisions"])
    assert all(row["retention"]["kind"] == "revision" for row in data["revisions"])
    view = await user_client.get(f"/api/v1/skills/state/checkpoints/{old}")
    assert view.status_code == 200, view.text
    assert view.json()["data"]["retention"]["id"] == str(old)
    assert await persisted_content(stopped) == before
    last, _ = await full_preview(stopped, tmp_path, "diagnostic-fixture")
    assert last.confirmation is not None
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path, "diagnostic-fixture").execute(
            stopped.owner,
            PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation),
        )
        tasks = tuple(
            await session.scalars(
                select(SkillPruneOperationDeletion.deletion_id).where(
                    SkillPruneOperationDeletion.operation_id == receipt.operation_id
                )
            )
        )
    pending = (await user_client.get("/api/v1/skills/storage")).json()["data"]
    assert pending["deletion"]["pending_tasks"] == len(tasks) > 0
    assert pending["deletion"]["pending_file_bytes"] == receipt.summary.pending_file_bytes > 0
    worker = SkillContentDeletionWorker(stopped.database, PrivateObjectStore(tmp_path / "objects"))
    for task in tasks:
        assert await worker.process(task) == "complete"
    final = (await user_client.get("/api/v1/skills/storage")).json()["data"]
    assert final["state_bytes"] == pending["state_bytes"]
    assert final["deletion"]["pending_file_bytes"] == 0
    assert final["deletion"]["cumulative_deleted_bytes"] == receipt.summary.pending_file_bytes
    expired = (await user_client.get(f"/api/v1/skills/state/checkpoints/{old}")).json()["data"]
    assert expired["retention"]["state"] == "retired"
    assert expired["retention"]["expires_at"] is None
    accepted = await user_client.get(
        f"/api/v1/skills/state/prune/operations/{receipt.operation_id}"
    )
    assert accepted.json()["data"] == receipt.model_dump(mode="json")


@pytest.mark.parametrize("kind", ["other-user", "device", "node", "disabled"])
async def test_diagnostics_authorization_and_zero_user_isolation(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    kind: str,
) -> None:
    """
    存储只解释本人，详情对象仍需所有权，设备节点和关闭功能不开放诊断。

    :param user_client (AsyncClient): 真实认证客户端
    :param stopped (RuntimeHarness): 原始账户
    :param kind (str): 待验证身份或开关
    """
    info = (await user_client.get("/api/v1/skills/installations/learning")).json()["data"]
    headers = {}
    if kind == "disabled":
        assert isinstance(user_client._transport, ASGITransport)
        app = user_client._transport.app
        assert isinstance(app, FastAPI)
        app.dependency_overrides[get_settings] = lambda: Settings(
            secret_key="skill-resolution-test", skill_manager_enabled=False
        )
    else:
        owner = await user(stopped.database) if kind == "other-user" else stopped.owner
        value = await token(stopped, owner, "user" if kind == "other-user" else kind)
        headers = {"Authorization": "Bearer " + value}
    storage = await user_client.get("/api/v1/skills/storage", headers=headers)
    assert (
        storage.status_code
        == {"other-user": 200, "device": 403, "node": 401, "disabled": 503}[kind]
    )
    if kind == "other-user":
        assert storage.json()["data"]["package_bytes"] == 0
        assert storage.json()["data"]["state_bytes"] == 0
    detail = await user_client.get("/api/v1/skills/installations/" + info["id"], headers=headers)
    assert (
        detail.status_code == {"other-user": 404, "device": 403, "node": 401, "disabled": 503}[kind]
    )


async def test_real_upload_reservations_are_separate_and_query_does_not_expire_them(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """
    真实包和状态上传独立预留，诊断不执行过期清理或把预留当作已保留字节。

    :param database (async_sessionmaker[AsyncSession]): 独立数据库
    :param tmp_path (Path): 私有内容卷
    """
    owner = await user(database)
    async with database.begin() as session:
        content = content_service(session, tmp_path)
        await content.begin(
            owner, "package", SkillTreeManifest(entries=(file_entry(b"package"),)), "package"
        )
        await content.begin(
            owner, "state", SkillTreeManifest(entries=(file_entry(b"state"),)), "state"
        )
    async with database.begin() as session:
        view = await SkillDiagnosticService(session, SkillStoragePolicy()).storage_view(owner)
        assert view.package_bytes == view.state_bytes == 0
        assert view.package_reserved_bytes == 7 and view.state_reserved_bytes == 5
        assert not session.new and not session.dirty and not session.deleted


async def test_archived_revision_uses_configured_archive_period(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    无会话的新安装卸载后使用归档期限，诊断保留真实原释放时间。

    :param stopped (RuntimeHarness): 授权账户
    :param tmp_path (Path): 私有卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    installed = await library.add(await library.candidate("archive"))
    assert installed.data is not None
    identity = installed.data.skill_ids[0]
    revision = installed.data.revision_ids[0]
    await library.execute(
        SkillRemoveRequest(
            skill=str(identity),
            expected_generation=installed.data.generation,
            idempotency_key=str(uuid4()),
        )
    )
    async with stopped.database.begin() as session:
        row = await session.get(SkillInstallation, identity)
        assert row is not None and row.removed
        result = await SkillDiagnosticService(
            session, SkillStoragePolicy(history_days=3, archive_days=17)
        ).histories(stopped.owner, "revision", (revision,))
        value = result[revision]
        assert value.archived and value.retention_days == 17 and value.state == "waiting"
        assert value.released_at is not None and value.expires_at == value.released_at + timedelta(
            days=17
        )


async def test_retry_and_shared_categories_do_not_double_count_physical_files(
    database: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    同摘要两分类只删除一个文件，真实失败后的重试数与完成累计量保持独立。

    :param database (async_sessionmaker[AsyncSession]): 独立数据库
    :param tmp_path (Path): 私有内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入磁盘失败
    """
    owner, identity, _ = await pending_deletion(database, tmp_path, both=True)
    store = PrivateObjectStore(tmp_path / "objects")
    original = store.delete_committed

    async def fail(user_id: UUID, digest: str, size: int, task_id: UUID) -> None:
        """
        模拟私有 I/O 错误，不把正文加入诊断。

        :param user_id (UUID): 用户身份
        :param digest (str): 文件摘要
        :param size (int): 长度
        :param task_id (UUID): 原任务
        """
        raise OSError("private path must not leak")

    monkeypatch.setattr(store, "delete_committed", fail)
    worker = SkillContentDeletionWorker(database, store)
    assert await worker.process(identity) == "pending"
    async with database.begin() as session:
        pending = await SkillDiagnosticService(session, SkillStoragePolicy()).storage_view(owner)
        assert pending.package_bytes == pending.state_bytes == 0
        assert pending.deletion.pending_tasks == pending.deletion.retrying_tasks == 1
        assert pending.deletion.pending_file_bytes == len(b"retained content")
        assert "private path" not in pending.model_dump_json()
    monkeypatch.setattr(store, "delete_committed", original)
    await make_due(database, identity)
    assert await worker.process(identity) == "complete"
    async with database.begin() as session:
        completed = await SkillDiagnosticService(session, SkillStoragePolicy()).storage_view(owner)
        assert completed.deletion.pending_tasks == completed.deletion.retrying_tasks == 0
        assert completed.deletion.completed_tasks == 1
        assert completed.deletion.cumulative_deleted_bytes == len(b"retained content")


async def test_postgres_storage_observation_blocks_concurrent_reservation(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    独立生产数据库连接证明观察持有真实用户锁，后续预留不会混入本次诊断。

    :param stopped (RuntimeHarness): 已有用户用量
    :param tmp_path (Path): 私有内容卷
    """
    import asyncio

    from sqlalchemy import func

    async with stopped.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    ready = asyncio.Event()
    writer_pid: list[int] = []

    async def reserve() -> None:
        """
        新连接建立真实状态上传，需要相同用户锁。
        """
        async with stopped.database.begin() as writer:
            writer_pid.append(int(await writer.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            await content_service(writer, tmp_path).begin(
                stopped.owner,
                str(uuid4()),
                SkillTreeManifest(entries=(file_entry(b"diagnostic race"),)),
                "state",
            )

    task: asyncio.Task[None] | None = None
    try:
        async with stopped.database() as reader:
            service = SkillDiagnosticService(reader, SkillStoragePolicy())
            first = await service.storage_view(stopped.owner)
            task = asyncio.create_task(reserve())
            await asyncio.wait_for(ready.wait(), timeout=2)
            blocked = False
            async with stopped.database() as observer:
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
            again = await service.storage_view(stopped.owner)
            assert again.state_reserved_bytes == first.state_reserved_bytes
    finally:
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    async with stopped.database() as session:
        last = await SkillDiagnosticService(session, SkillStoragePolicy()).storage_view(
            stopped.owner
        )
        assert last.state_reserved_bytes == first.state_reserved_bytes + len(b"diagnostic race")
