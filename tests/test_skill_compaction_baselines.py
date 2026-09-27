"""
验证目录整理不改写真实增量迁移基线，等价 head 替换之后仍可继续迁移。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import directory_tree
from skill_runtime_support import RuntimeHarness
from test_skill_compaction_projection import mapped_protection, projected
from test_skill_content_service import database as database
from test_skill_directory_compaction import apply, preview, shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_history_retirement import retire
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_migration import (
    SkillMigrationRequest,
    SkillMigrationSelector,
    SkillMigrationView,
)
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration import SkillMigrationService
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def migrate_helper(
    state: RuntimeHarness, root: Path, source: UUID, target: UUID
) -> SkillMigrationView:
    """
    通过真实显式迁移服务推进 helper，固定来源版本而不借用当前账户 pin。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param source (UUID): 来源原始版本
    :param target (UUID): 目标原始版本
    :return SkillMigrationView: 完整提交的增量结果
    """
    async with state.database.begin() as session:
        service = SkillMigrationService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        selector = SkillMigrationSelector(
            account_id=state.account,
            skill="helper",
            from_revision=str(source),
            to_revision=str(target),
        )
        request = SkillMigrationRequest(
            selector=selector,
            expected=await service.selection.current(state.owner, selector),
            idempotency_key=str(uuid4()),
        )
        return await service.execute(state.owner, request)


async def test_compaction_keeps_exact_successful_baseline_and_next_increment(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    相同学习内容的新 head 不替换基线身份，旧基线仍阻止退役，后续真实增量可以继续。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    old, helper_head = await shared_directory(stopped, tmp_path)
    helper_revision = (
        (await command(stopped, tmp_path, skill="helper")).expected.targets[0].revision_id
    )
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillUpdateRequest(
            skill="helper",
            item=await library.candidate(name="helper", version="new helper upstream"),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    target = (await command(stopped, tmp_path, skill="helper")).expected.targets[0].revision_id
    first = await migrate_helper(stopped, tmp_path, helper_revision, target)
    assert first.operation_id is not None and first.status == "ready"
    for scope, revision in (
        (SkillScope(tools=("claude",)), target),
        (SkillScope(account_id=stopped.account), helper_revision),
    ):
        await library.execute(
            SkillRuleRequest(
                command="pin",
                skill="helper",
                revision=str(revision),
                scope=scope,
                expected_generation=await library.generation(),
                idempotency_key=str(uuid4()),
            )
        )
    async with stopped.database() as session:
        migration = await session.get(SkillBranchPreparation, first.operation_id)
        assert migration is not None and migration.source_checkpoint_id == helper_head
        original = (
            migration.source_checkpoint_id,
            migration.current_checkpoint_id,
            migration.response_json,
        )
    plan = await preview(stopped, tmp_path, old)
    predicted = await projected(stopped, tmp_path, plan)
    assert predicted.after.reasons("migration_baseline", first.operation_id)
    assert predicted.after.reasons("checkpoint", helper_head)
    assert RetentionKey("checkpoint", str(helper_head)) not in {
        row.key for row in predicted.newly_released
    }
    async with stopped.database() as session:
        _, retirement = await SkillDirectoryCompactionService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).retirement_preview(stopped.owner, plan)
        assert not retirement.ready
        baseline_entry = next(
            row
            for row in retirement.entries
            if row.key == RetentionKey("checkpoint", str(helper_head))
        )
        assert "protected" in baseline_entry.blockers
    result = await apply(stopped, tmp_path, plan)
    assert helper_head in dict(result.head_replacements)
    async with stopped.database() as session:
        migration = await session.get(SkillBranchPreparation, first.operation_id)
        assert migration is not None
        assert (
            migration.source_checkpoint_id,
            migration.current_checkpoint_id,
            migration.response_json,
        ) == original
        protected = await SkillRetentionInspector(session).inspect(stopped.owner)
        assert mapped_protection(predicted, result) == protected
        assert protected.reasons("migration_baseline", first.operation_id)
        assert protected.reasons("checkpoint", helper_head)
    with pytest.raises(SkillContentError) as error:
        await retire(stopped, RetentionKey("checkpoint", str(helper_head)), early=True)
    assert error.value.code == "STATE_PROTECTED"
    second = await migrate_helper(stopped, tmp_path, helper_revision, target)
    assert second.status == "ready" and second.before.last_migration_id == first.operation_id
    assert second.migration_sequence == 2
    assert "learning" not in {
        entry.path.split("/", 1)[0] for entry in (await directory_tree(stopped, tmp_path)).entries
    }
