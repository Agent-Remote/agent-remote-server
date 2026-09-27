"""
验证真实迁移记录的只读候选、原始包覆盖与人工内容权限和故障边界。
"""

from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_migration import migrate, request, versions
from test_skill_migration_conflicts import BASE, counts, pending
from test_skill_migration_resolution_content import begin
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionOperation,
    SkillMigrationResolutionPlan,
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_plan import (
    MigrationResolutionCalculation,
    MigrationResolutionPlanner,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def calculate(
    state: RuntimeHarness,
    root: Path,
    identity: UUID,
    choices: list[SkillResolutionChoice],
    policy: SkillStoragePolicy | None = None,
) -> MigrationResolutionCalculation:
    """
    独立请求事务计算候选并提交，以便发现任何不应存在的只读写入。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 私有内容卷
    :param identity (UUID): 原迁移身份
    :param choices (list[SkillResolutionChoice]): 完整明确计划
    :param policy (SkillStoragePolicy | None): 可选限额
    :return MigrationResolutionCalculation: 候选及覆盖说明
    """
    async with state.database.begin() as session:
        return await MigrationResolutionPlanner(
            session, PrivateObjectStore(root / "objects"), policy or SkillStoragePolicy()
        ).calculate(state.owner, identity, choices)


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_original_overwrites_are_explicit_and_calculation_is_read_only(
    stopped: RuntimeHarness,
    tmp_path: Path,
    mode: str,
) -> None:
    """
    选择旧完整树保留目标版本身份但标记 modified，不修改原始回执、计划或基线。

    :param stopped (RuntimeHarness): 原会话
    :param tmp_path (Path): 私有卷
    :param mode (str): 首次或显式迁移
    """
    saved = await pending(stopped, tmp_path, mode)
    identity = saved.operation_id
    assert identity is not None
    before = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        usage_before = (usage.lock_version, usage.state_bytes, usage.state_reserved)
    incoming = await calculate(stopped, tmp_path, identity, [SkillResolutionChoice(use="incoming")])
    assert incoming.result.merged is not None and incoming.target_modified
    assert incoming.target_revision_id == before.targets[0].revision_id
    assert incoming.original_changes is not None
    assert {change.path for change in incoming.original_changes} == {
        "learning/SKILL.md",
        "learning/one",
    }
    assert incoming.target_changes == incoming.original_changes
    assert incoming.other_changed_roots == ()
    current = await calculate(stopped, tmp_path, identity, [SkillResolutionChoice(use="current")])
    assert current.result.merged is not None and current.target_modified is False
    assert current.original_changes == [] and current.target_changes == []
    assert (await command(stopped, tmp_path)).expected == before
    assert await counts(stopped) == rows
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert (
            usage is not None
            and (usage.lock_version, usage.state_bytes, usage.state_reserved) == usage_before
        )
        row = await session.get(SkillBranchPreparation, identity)
        assert row is not None and row.response_json == saved.model_dump(mode="json")
        assert row.migration_sequence is None and row.status == "conflicted"
        assert await session.get(SkillMigrationResolutionPlan, identity) is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillMigrationResolutionOperation)
                .where(SkillMigrationResolutionOperation.migration_id == identity)
            )
            == 0
        )


async def test_unresolved_plan_never_reports_candidate_or_baseline_differences(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    尚未选择全部冲突时，结果与修改标记均为空，不暴露部分树。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    result = await calculate(stopped, tmp_path, saved.operation_id, [])
    assert result.result.merged is None and result.result.conflicts == saved.conflicts
    assert result.target_modified is None and result.target_changes is None
    assert result.original_changes is None and result.directory_changes is None


async def test_existing_target_state_is_not_confused_with_target_original(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    保留目标已学状态相对自身可以无变化，但相对原始包仍明确标记 modified。

    :param stopped (RuntimeHarness): 原旧版账户
    :param tmp_path (Path): 私有内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"one"}))
    late = await new_session(stopped, tmp_path)
    source, target = await versions(stopped, tmp_path)
    await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    newer = await new_session(stopped, tmp_path)
    await publish(newer, tmp_path, await ingest(newer, tmp_path, {"learning/memory": b"target"}))
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/memory": b"source"}))
    saved = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    assert saved.operation_id is not None and saved.status == "conflicted"
    result = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="current")]
    )
    assert result.target_revision_id == target and result.target_modified
    assert result.target_changes == [] and result.original_changes is not None
    assert [change.path for change in result.original_changes] == ["learning/memory"]
    assert result.info.current.checkpoint_id == saved.before.target.checkpoint_id


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_linked_runtime_candidate_keeps_context_and_refuses_path_selection(
    stopped: RuntimeHarness,
    tmp_path: Path,
    mode: str,
) -> None:
    """
    真实保存的关联输入可整体选择，辅助状态随 current 上下文保留且不能逐文件绕过。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param mode (str): 原迁移模式
    """
    saved = await pending(stopped, tmp_path, mode, linked=True)
    assert saved.operation_id is not None
    result = await calculate(
        stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="current")]
    )
    assert result.unit == (".", "learning") and result.target_modified is False
    assert result.result.merged is not None
    paths = {entry.path for entry in result.result.merged.entries}
    assert {"aux", "unrelated", "learning/SKILL.md"} <= paths
    assert "learning/link" not in paths
    with pytest.raises(SkillContentError) as error:
        await calculate(
            stopped,
            tmp_path,
            saved.operation_id,
            [SkillResolutionChoice(path="learning", use="incoming")],
        )
    assert error.value.code == "INVALID_RESOLUTION"


async def test_real_custom_file_must_be_completed_for_exact_migration(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    人工文件保持其他学习数据，其他迁移的相同树仍不可作为本计划输入。

    :param user_client (AsyncClient): 认证客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert isinstance(saved, SkillMigrationView) and saved.operation_id is not None
    upload = await begin(user_client, saved.operation_id)
    path = f"{BASE}/{saved.operation_id}/uploads/{upload}"
    entry = file_entry(b"resolved")
    assert (
        await user_client.put(path + "/files/" + entry.sha256, content=b"resolved")
    ).status_code == 200
    completed = await user_client.post(path + "/complete")
    choice = SkillResolutionChoice(
        path="learning/SKILL.md", file_tree_digest=completed.json()["data"]["tree_digest"]
    )
    result = await calculate(stopped, tmp_path, saved.operation_id, [choice])
    assert result.result.merged is not None and result.target_modified
    entries = {entry.path: entry for entry in result.result.merged.entries}
    assert entries["learning/SKILL.md"].sha256 == entry.sha256 and "learning/one" in entries
    other = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped, tmp_path, saved.before.source.revision_id, saved.before.target.revision_id
        ),
    )
    assert other.operation_id is not None
    with pytest.raises(SkillContentError) as error:
        await calculate(stopped, tmp_path, other.operation_id, [choice])
    assert error.value.code == "RESOLUTION_CONTENT_NOT_FOUND"


@pytest.mark.parametrize("changed", ["reset", "source", "configuration"])
async def test_changed_conditions_require_recomputation_before_candidate(
    stopped: RuntimeHarness,
    tmp_path: Path,
    changed: str,
) -> None:
    """
    重置或真实目录配置变化不能用旧候选继续预览可提交结果。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param changed (str): 需要失效的条件
    """
    saved = await pending(stopped, tmp_path)
    assert isinstance(saved, SkillMigrationView) and saved.operation_id is not None
    if changed == "reset":
        await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    elif changed == "configuration":
        await pin(stopped, tmp_path, saved.before.source.revision_id)
    else:
        await pin(stopped, tmp_path, saved.before.source.revision_id)
        late = await new_session(stopped, tmp_path)
        await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/late": b"late"}))
    with pytest.raises(SkillContentError) as error:
        await calculate(
            stopped, tmp_path, saved.operation_id, [SkillResolutionChoice(use="incoming")]
        )
    assert error.value.code == (
        "CONFLICT_NOT_ACTIVE" if changed == "reset" else "STATE_PRECONDITION_CHANGED"
    )


@pytest.mark.parametrize("damage", ["missing", "corrupt", "quota", "other-user"])
async def test_candidate_checks_actual_content_quota_and_owner(
    stopped: RuntimeHarness,
    tmp_path: Path,
    damage: str,
) -> None:
    """
    只读计算也验证实际内容与额度，不因无写入而放宽授权或返回虚假成功。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param damage (str): 校验失败类型
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    if damage in {"missing", "corrupt"}:
        entry = file_entry(b"learned")
        target = tmp_path / "objects" / str(stopped.owner) / entry.sha256[:2] / entry.sha256
        if damage == "missing":
            target.unlink()
        else:
            target.chmod(0o600)
            target.write_bytes(b"broken")
    with pytest.raises(SkillContentError) as error:
        if damage == "other-user":
            owner = await user(stopped.database)
            async with stopped.database.begin() as session:
                await MigrationResolutionPlanner(
                    session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
                ).calculate(owner, saved.operation_id, [])
        else:
            await calculate(
                stopped,
                tmp_path,
                saved.operation_id,
                [SkillResolutionChoice(use="incoming")],
                SkillStoragePolicy(checkpoint_bytes=1) if damage == "quota" else None,
            )
    assert (
        error.value.code
        == {
            "missing": "CONTENT_INCOMPLETE",
            "corrupt": "CONTENT_INVALID",
            "quota": "QUOTA_EXCEEDED",
            "other-user": "CONFLICT_NOT_FOUND",
        }[damage]
    )
