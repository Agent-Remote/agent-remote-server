"""
验证历史退役与内容回收可组合为单个业务提交，最后才由独立 worker 删除磁盘文件。
"""

import hashlib
from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_checkpoint_retirement import historical_selection
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.services.skills.gc import (
    ContentReclamationResult,
    SkillContentReclamationService,
)
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_history_retirement_quota_and_deletion_tasks_share_one_outer_savepoint(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    已退役审计保持原身份，内容结算失败整体回滚；提交后 worker 才真正删除旧学习文件。

    :param stopped (RuntimeHarness): 已有真实发布和账户的夹具
    :param tmp_path (Path): 私有内容卷
    """
    selected, head_id, _ = await historical_selection(stopped, tmp_path)
    policy = SkillStoragePolicy()
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        head = await session.get(SkillCheckpoint, head_id)
        assert usage is not None and head is not None
        original_usage = (usage.package_bytes, usage.state_bytes)
        original_digest = head.content_digest

    async def release(session: AsyncSession) -> ContentReclamationResult:
        """
        在同一已授权用户事务里先退役全部历史，再验证真正无消费者的状态树。

        :param session (AsyncSession): 调用方外层事务
        :return ContentReclamationResult: 逻辑结算和原删除任务
        """
        await SkillHistoryRetirementService(session, policy).retire(
            stopped.owner,
            stopped.account,
            selected,
            all_unreferenced=True,
        )
        views = await SkillRetentionInspector(session).trees(stopped.owner, policy)
        trees = tuple(
            row.key
            for row in views
            if row.key.kind == "state_tree" and not row.references and not row.reasons
        )
        assert trees
        service = SkillContentReclamationService(session, policy)
        plan = await service.preview(stopped.owner, trees, all_unreferenced=True)
        assert plan.ready and plan.state_bytes > 0 and plan.package_bytes == 0
        return await service.apply(stopped.owner, plan)

    async with stopped.database.begin() as session:
        with pytest.raises(RuntimeError, match="outer rejection"):
            async with retention_mutation(session, stopped.owner):
                await release(session)
                raise RuntimeError("outer rejection")
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        head = await session.get(SkillCheckpoint, head_id)
        assert usage is not None and (usage.package_bytes, usage.state_bytes) == original_usage
        assert head is not None and head.retained and head.tree_digest == original_digest
        assert not list(
            await session.scalars(
                select(SkillContentDeletion).where(SkillContentDeletion.user_id == stopped.owner)
            )
        )
    async with stopped.database.begin() as session, retention_mutation(session, stopped.owner):
        result = await release(session)
    digest = hashlib.sha256(b"learned").hexdigest()
    path = tmp_path / "objects" / str(stopped.owner) / digest[:2] / digest
    assert path.read_bytes() == b"learned"
    worker = SkillContentDeletionWorker(stopped.database, PrivateObjectStore(tmp_path / "objects"))
    assert result.deletion_ids
    for identity in result.deletion_ids:
        assert await worker.process(identity) == "complete"
    assert not path.exists()
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        head = await session.get(SkillCheckpoint, head_id)
        assert usage is not None and usage.package_bytes == original_usage[0]
        assert usage.state_bytes == original_usage[1] - result.state_bytes
        assert head is not None and not head.retained and head.content_digest == original_digest
