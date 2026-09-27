"""
验证主动失效与原操作保存点一致，外层捕获错误后仍可安全提交其他工作。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate, request
from test_skill_migration_conflicts import counts, pending
from test_skill_migration_recomputation import info
from test_skill_migration_resolution_drafts import edit_request, saved_plan
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionOperation
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.schemas.skill_results import SkillOperationTarget
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library import SkillLibraryService
from agent_remote_server.services.skills.library_context import LibraryChange
from agent_remote_server.services.skills.migration_resolution import SkillMigrationResolutionService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_library_late_failure_rolls_back_removal_and_proactive_supersession(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    失效执行后的部署目标计算失败不能留下已取消计划或已提交移除。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 库事务晚到故障
    """
    original = await pending(stopped, tmp_path)
    assert original.operation_id is not None
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    generation = await library.generation()
    payload = SkillRemoveRequest(
        skill="learning", idempotency_key=str(uuid4()), expected_generation=generation
    )
    targets = SkillLibraryService._targets

    async def fail(
        self: SkillLibraryService, user_id: UUID, change: LibraryChange
    ) -> list[SkillOperationTarget]:
        """
        主动失效之后模拟受理响应构造失败。

        :param user_id (UUID): 当前用户
        :param change (LibraryChange): 已变更安装
        :return list[SkillOperationTarget]: 故障注入不会返回
        """
        await targets(self, user_id, change)
        raise SkillContentError("INJECTED_FAILURE", "late library failure")

    with monkeypatch.context() as patch:
        patch.setattr(SkillLibraryService, "_targets", fail)
        async with stopped.database.begin() as session:
            with pytest.raises(SkillContentError, match="late library failure"):
                await library.service(session).execute(stopped.owner, payload)
    current = await info(stopped, tmp_path, original.operation_id)
    assert current.status == "conflicted" and current.superseded_reason is None
    assert current.original == original and await library.generation() == generation
    assert not (await library.info()).removed
    await library.execute(payload)
    assert (await info(stopped, tmp_path, original.operation_id)).status == "superseded"


async def test_resolution_receipt_failure_restores_competing_conflicts_and_all_heads(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    最后回执失败回滚成功记录、被取代冲突、计划版本与目录，不依赖外层回滚。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 回执故障注入
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    newer = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped,
            tmp_path,
            original.before.source.revision_id,
            original.before.target.revision_id,
        ),
    )
    assert newer.operation_id is not None
    heads = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    save = SkillMigrationResolutionRepository.save_operation

    async def fail(
        self: SkillMigrationResolutionRepository, operation: SkillMigrationResolutionOperation
    ) -> None:
        """
        在发布、成功替代和回执都已写入后触发异常。

        :param operation (SkillMigrationResolutionOperation): 本次完整解决回执
        """
        await save(self, operation)
        raise SkillContentError("INJECTED_FAILURE", "late resolution failure")

    with monkeypatch.context() as patch:
        patch.setattr(SkillMigrationResolutionRepository, "save_operation", fail)
        async with stopped.database.begin() as session:
            service = SkillMigrationResolutionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            )
            with pytest.raises(SkillContentError, match="late resolution failure"):
                await service.execute(
                    stopped.owner,
                    newer.operation_id,
                    edit_request(SkillResolutionChoice(use="current")),
                )
    assert await counts(stopped) == rows and (await command(stopped, tmp_path)).expected == heads
    for identity in (original.operation_id, newer.operation_id):
        current = await info(stopped, tmp_path, identity)
        assert (
            current.status == "conflicted"
            and current.replacement_id is None
            and current.superseded_reason is None
        )
        assert await saved_plan(stopped, identity) == (0, [], 0)
