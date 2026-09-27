"""
验证只读空间预测与整份整理、历史退役、额度结算的真实原子组合。
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_gc import upload
from test_skill_content_service import database as database
from test_skill_content_service import service
from test_skill_directory_compaction import fingerprint, preview, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.db import Base
from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillStorageUsage,
    SkillStoredTree,
)
from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.compaction.plan import CompactionPlan
from agent_remote_server.services.skills.compaction.reclamation import CompactionReclamationPreview
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def persisted_content(state: RuntimeHarness) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """
    读取当前用户全部业务表完整列值，防止只检查头和行数遗漏预览的隐藏写入。

    :param state (RuntimeHarness): 隔离测试账户
    :return tuple[tuple[str, tuple[str, ...]], ...]: 完整持久化值指纹
    """
    result = []
    async with state.database() as session:
        for table in sorted(Base.metadata.tables.values(), key=lambda row: row.name):
            if "user_id" not in table.c:
                continue
            rows = await session.execute(
                select(table)
                .where(table.c.user_id == state.owner)
                .order_by(*table.primary_key.columns)
            )
            result.append((table.name, tuple(repr(tuple(row)) for row in rows)))
    return tuple(result)


async def forecast(
    state: RuntimeHarness, root: Path, plan: CompactionPlan
) -> CompactionReclamationPreview:
    """
    提交预览事务并检查 ORM 不含任何暂存写入。

    :param state (RuntimeHarness): 原始账户夹具
    :param root (Path): 私有内容卷
    :param plan (CompactionPlan): 原始精确整理选择
    :return CompactionReclamationPreview: 完整只读预测
    """
    before = await persisted_content(state)
    async with state.database.begin() as session:
        result = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).reclamation_preview(state.owner, plan)
        assert not session.new and not session.dirty and not session.deleted
        await session.flush()
    assert await persisted_content(state) == before
    return result


async def complete_selection(state: RuntimeHarness, root: Path, old: UUID) -> CompactionPlan:
    """
    显式选择同份旧目录的全部无保护单项输入，保留完整恢复损失以供审阅。

    :param state (RuntimeHarness): 已发布账户
    :param root (Path): 内容卷
    :param old (UUID): 已解除保护的原始分支头
    :return CompactionPlan: 包含原上传输入的精确整理选择
    """
    async with state.database() as session:
        index = await SkillRetentionRepository(session).load(state.owner)
        original = next(row for row in index.checkpoints if row.id == old)
        protected = protection(index, datetime.now(UTC))
        selected = tuple(
            row.id
            for row in index.checkpoints
            if row.scope == "item"
            and row.tree_digest == original.tree_digest
            and not protected.reasons("checkpoint", row.id)
        )
    return await preview(state, root, *selected)


@pytest.mark.parametrize("lease", ["none", "state", "package"])
async def test_forecast_matches_atomic_result_and_preserves_replacement_bytes(
    stopped: RuntimeHarness, tmp_path: Path, lease: str
) -> None:
    """
    新树保住共享内容，分类租约正确影响额度和磁盘预测，无关未绑定上传不进入删除范围。

    :param stopped (RuntimeHarness): 已有真实发布的账户
    :param tmp_path (Path): 私有对象卷
    :param lease (str): 历史文件额外活动租约类别
    """
    old, keep = await shared_directory(stopped, tmp_path)
    _, unrelated, _ = await upload(stopped.database, tmp_path, stopped.owner, data=b"unbound")
    pending_id = None
    if lease != "none":
        async with stopped.database.begin() as session:
            pending = await service(session, tmp_path).begin(
                stopped.owner,
                str(uuid4()),
                SkillTreeManifest(entries=(file_entry(b"historical", path="memory"),)),
                "state" if lease == "state" else "package",
            )
            assert pending.reserved_bytes == (0 if lease == "state" else len(b"historical"))
            pending_id = pending.id
    plan = await complete_selection(stopped, tmp_path, old)
    before = await fingerprint(stopped)
    predicted = await forecast(stopped, tmp_path, plan)
    assert await fingerprint(stopped) == before
    content = predicted.content
    assert predicted.ready and content is not None and content.requested_trees, str(predicted.trees)
    assert unrelated not in content.requested_trees
    historical = next(
        row for row in content.blobs if row.digest == hashlib.sha256(b"historical").hexdigest()
    )
    assert historical.objects[0].release == (lease != "state")
    assert historical.delete_file == (lease == "none")
    current = next(
        row for row in content.blobs if row.digest == hashlib.sha256(b"current").hexdigest()
    )
    assert not current.delete_file and not current.objects[0].release
    assert set(current.objects[0].tree_keys) - set(content.requested_trees)
    async with stopped.database.begin() as session:
        result = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).apply_reclamation(stopped.owner, predicted)
    assert result.content.state_bytes == content.state_bytes
    assert result.content.package_bytes == content.package_bytes == 0
    assert result.content.pending_file_bytes == content.pending_file_bytes
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert isinstance(before[1], int)
        assert usage is not None and usage.state_bytes == before[1] - content.state_bytes
        assert await session.get(SkillStoredTree, (stopped.owner, "state", unrelated.identity))
        retained = await session.get(
            SkillCheckpoint, dict(result.compaction.head_replacements)[keep]
        )
        original = await session.get(SkillCheckpoint, keep)
        assert retained is not None and retained.retained
        assert original is not None and not original.retained
    if pending_id is not None:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).complete(stopped.owner, pending_id)


async def test_compaction_reclamation_last_flush_failure_rolls_back_every_stage(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    真实删除任务 flush 后失败，捕获并提交外层仍恢复全部头、历史、树和额度。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入最后一步失败
    """
    old, _ = await shared_directory(stopped, tmp_path)
    predicted = await forecast(stopped, tmp_path, await complete_selection(stopped, tmp_path, old))
    assert predicted.content is not None and predicted.content.pending_file_bytes > 0
    before = await fingerprint(stopped)
    original = SkillContentGCRepository.flush
    calls = 0

    async def reject(repository: SkillContentGCRepository) -> None:
        """
        保持真实 SQL 执行直到最后一次任务写入，然后触发整个业务保存点回滚。

        :param repository (SkillContentGCRepository): 当前事务仓储
        """
        nonlocal calls
        await original(repository)
        calls += 1
        if calls == 2:
            raise RuntimeError("after deletion flush")

    monkeypatch.setattr(SkillContentGCRepository, "flush", reject)
    async with stopped.database.begin() as session:
        with pytest.raises(RuntimeError, match="after deletion flush"):
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply_reclamation(stopped.owner, predicted)
    assert calls == 2 and (await fingerprint(stopped))[1:] == before[1:]
    async with stopped.database() as session:
        assert not list(
            await session.scalars(
                select(SkillContentDeletion).where(SkillContentDeletion.user_id == stopped.owner)
            )
        )
        for key in predicted.content.requested_trees:
            assert await session.get(SkillStoredTree, (stopped.owner, "state", key.identity))
        for blob in predicted.content.blobs:
            obj = await session.get(SkillContentObject, (stopped.owner, "state", blob.digest))
            assert obj is not None and obj.status == "available"


async def test_blocked_history_refuses_whole_composition_without_partial_compaction(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    普通模式新解除保护的消费者仍等待；完整历史有阻断时不执行任何容易删除的子集。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, old)
        assert checkpoint is not None
        checkpoint.retention_released_at = datetime.now(UTC) - timedelta(days=31)
    predicted = await forecast(
        stopped, tmp_path, await preview(stopped, tmp_path, old, early=False)
    )
    assert not predicted.ready and predicted.content is None and not predicted.trees
    before = await fingerprint(stopped)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply_reclamation(stopped.owner, predicted)
        assert error.value.code == "STATE_PROTECTED"
    assert (await fingerprint(stopped))[1:] == before[1:]


async def test_new_zero_reservation_upload_invalidates_full_forecast(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    确认期间新增零额度租约也必须使整个整理计划失效，不能提前发布新头。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    predicted = await forecast(stopped, tmp_path, await complete_selection(stopped, tmp_path, old))
    async with stopped.database.begin() as session:
        accepted = await service(session, tmp_path).begin(
            stopped.owner,
            str(uuid4()),
            SkillTreeManifest(entries=(file_entry(b"historical", path="memory"),)),
            "state",
        )
        assert accepted.reserved_bytes == 0
    before = await fingerprint(stopped)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply_reclamation(stopped.owner, predicted)
        assert error.value.code == "HEAD_CHANGED"
    assert (await fingerprint(stopped))[1:] == before[1:]
