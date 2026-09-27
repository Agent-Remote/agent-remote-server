"""
验证首次版本准备的来源选择、原子发布、保守冲突与独立幂等回执。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_preparation import (
    SkillBranchPreparation,
    SkillEffectiveBranch,
)
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_preparation import (
    SkillPreparationRequest,
    SkillPreparationView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.preparation import SkillPreparationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def update_version(state: RuntimeHarness, root: Path, version: str) -> UUID:
    """
    登记并激活一个内容不同的新原始版本，不准备任何账户分支。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 私有内容卷
    :param version (str): 不同上游内容
    :return UUID: 新登记版本身份
    """
    library = LibraryHarness(state.database, root, state.owner)
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version=version),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    revision = (await library.info()).default_revision_id
    assert revision is not None
    return revision


async def request(state: RuntimeHarness, root: Path) -> SkillPreparationRequest:
    """
    使用真实只读当前选择构造待确认的准备请求。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :return SkillPreparationRequest: 完整前置条件及新键
    """
    current = await command(state, root)
    return SkillPreparationRequest(
        idempotency_key=str(uuid4()), selector=current.selector, expected=current.expected
    )


async def prepare(
    state: RuntimeHarness,
    root: Path,
    payload: SkillPreparationRequest,
    policy: SkillStoragePolicy | None = None,
) -> SkillPreparationView:
    """
    用独立提交事务模拟重启和断线后的真实准备操作。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param payload (SkillPreparationRequest): 明确原始请求
    :param policy (SkillStoragePolicy | None): 可选配额约束
    :return SkillPreparationView: 完整已提交受理结果
    """
    async with state.database.begin() as session:
        return await SkillPreparationService(
            session, PrivateObjectStore(root / "objects"), policy or SkillStoragePolicy()
        ).execute(state.owner, payload)


async def pin(state: RuntimeHarness, root: Path, revision: UUID | None) -> None:
    """
    改变账户版本规则，不直接变更任何运行态 head。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param revision (UUID | None): 固定版本或取消固定
    """
    library = LibraryHarness(state.database, root, state.owner)
    await library.execute(
        SkillRuleRequest(
            command="pin" if revision else "unpin",
            skill="learning",
            revision=str(revision) if revision else None,
            scope=SkillScope(account_id=state.account),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )


async def test_initial_preparation_and_snapshot_history_are_separate(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    准备成功才有完整 head，但只有真实完整预约才记录最近使用分支。

    :param prepared (RuntimeHarness): 尚未使用分支
    :param tmp_path (Path): 内容卷
    """
    result = await prepare(prepared, tmp_path, await request(prepared, tmp_path))
    assert result.status == "ready" and result.mode == "initial"
    async with prepared.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillEffectiveBranch)
                .where(SkillEffectiveBranch.user_id == prepared.owner)
            )
            == 0
        )
    snapshot = await reserve(prepared, tmp_path)
    async with prepared.database() as session:
        ledger = await session.scalar(
            select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == prepared.owner)
        )
        assert ledger is not None and ledger.snapshot_id == snapshot.id
        assert ledger.state_id == result.target_state_id


async def test_forward_skips_unused_revisions_and_preserves_learning(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    自动来源跳过未使用版本，同时保留上游修改和独立学习文件。

    :param stopped (RuntimeHarness): 已使用旧版
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/memory": b"learned", "aux": b"keep"}),
    )
    old = (await command(stopped, tmp_path)).expected.targets[0]
    skipped = await update_version(stopped, tmp_path, "unused two")
    await update_version(stopped, tmp_path, "used three")
    payload = await request(stopped, tmp_path)
    async with stopped.database() as session:
        uploads = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == stopped.owner)
        )
    preview = await prepare(stopped, tmp_path, payload.model_copy(update={"dry_run": True}))
    assert preview.mode == "forward" and preview.status == "ready" and preview.operation_id is None
    assert preview.source_revision_id == old.revision_id
    assert preview.source_checkpoint_id == old.head_checkpoint_id
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillContentUpload)
                .where(SkillContentUpload.user_id == stopped.owner)
            )
            == uploads
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillBranchPreparation)
                .where(SkillBranchPreparation.user_id == stopped.owner)
            )
            == 0
        )
    result, replay = await asyncio.gather(
        prepare(stopped, tmp_path, payload), prepare(stopped, tmp_path, payload)
    )
    assert result == replay and result.result_tree_digest == preview.result_tree_digest
    paths = {entry.path for entry in (await directory_tree(stopped, tmp_path)).entries}
    assert paths == {"learning", "learning/SKILL.md", "learning/memory", "aux"}
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(AccountSkillState).where(AccountSkillState.base_revision_id == skipped)
            )
            is None
        )
        ledger = await session.scalar(
            select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == stopped.owner)
        )
        assert ledger is not None and ledger.state_id == old.state_id
        branch = await session.get(AccountSkillState, result.target_state_id)
        assert (
            branch is not None
            and branch.epoch == 1
            and branch.head_checkpoint_id == result.result_checkpoint_id
        )
    await new_session(stopped, tmp_path)
    async with stopped.database() as session:
        ledger = await session.scalar(
            select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == stopped.owner)
        )
        assert ledger is not None and ledger.state_id == result.target_state_id
    assert await prepare(stopped, tmp_path, payload) == result


async def test_conflict_is_durable_and_reset_supersedes_without_replaying(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同路径冲突保留三侧且无目标 head，显式重置后旧回执仍不可变。

    :param stopped (RuntimeHarness): 已使用分支
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/SKILL.md": b"learned instructions"}),
    )
    old = await directory_tree(stopped, tmp_path)
    await update_version(stopped, tmp_path, "upstream instructions")
    payload = await request(stopped, tmp_path)
    result = await prepare(stopped, tmp_path, payload)
    assert result.status == "conflicted" and result.result_checkpoint_id is None
    assert any(conflict.path == "learning/SKILL.md" for conflict in result.conflicts)
    assert await directory_tree(stopped, tmp_path) == old
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, result.target_state_id)
        row = await session.get(SkillBranchPreparation, result.operation_id)
        assert branch is not None and branch.head_checkpoint_id is None
        assert row is not None and row.source_checkpoint_id == result.source_checkpoint_id
    reset = await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert reset.superseded_conflicts == 1
    async with stopped.database() as session:
        receipt = await SkillPreparationService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).receipt(stopped.owner, payload.idempotency_key)
        assert receipt.current_status == "superseded" and receipt.result == result
    resumed = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert resumed.mode == "resume" and resumed.status == "ready"
    assert await prepare(stopped, tmp_path, payload) == result


async def test_old_pin_resumes_without_migration_and_unpin_migrates(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    固定旧分支不提前建立默认新版，取消固定后才执行必要迁移。

    :param stopped (RuntimeHarness): 已使用分支
    :param tmp_path (Path): 内容卷
    """
    old = (await command(stopped, tmp_path)).expected.targets[0]
    target = await update_version(stopped, tmp_path, "new")
    await pin(stopped, tmp_path, old.revision_id)
    resumed = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert resumed.mode == "resume" and resumed.result_checkpoint_id == old.head_checkpoint_id
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(AccountSkillState).where(AccountSkillState.base_revision_id == target)
            )
            is None
        )
    await pin(stopped, tmp_path, None)
    migrated = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert migrated.mode == "forward" and migrated.status == "ready"


async def test_older_unused_revision_initializes_original_with_warning(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    进入未使用旧版本明确不回灌较新版学习内容，已有分支仍优先复用。

    :param stopped (RuntimeHarness): 已使用原始分支
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"learned"})
    )
    unused = await update_version(stopped, tmp_path, "unused")
    await update_version(stopped, tmp_path, "latest")
    await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    await new_session(stopped, tmp_path)
    await pin(stopped, tmp_path, unused)
    result = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert result.status == "ready" and result.mode == "older"
    assert result.warnings == ("newer_state_not_migrated",)
    assert "learning/memory" not in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }


@pytest.mark.parametrize("linked", [False, True])
async def test_opaque_and_linked_state_never_automatically_mix(
    stopped: RuntimeHarness, tmp_path: Path, linked: bool
) -> None:
    """
    数据库双侧变更或外部链接关联保持完整冲突，不复制关联来源的一部分。

    :param stopped (RuntimeHarness): 旧会话
    :param tmp_path (Path): 内容卷
    :param linked (bool): 是否外部辅助根依赖
    """
    changes: dict[str, bytes | None] = (
        {"aux": b"data"} if linked else {"learning/memory.db": b"database"}
    )
    links = {"learning/link": "../aux"} if linked else None
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, changes, links=links))
    before = await directory_tree(stopped, tmp_path)
    await update_version(stopped, tmp_path, "upstream changes")
    result = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert result.status == "conflicted"
    assert result.conflicts[0].reason == ("source_conflict" if linked else "opaque_divergence")
    if linked:
        assert result.conflicts[0].unit == (".", "learning")
    assert await directory_tree(stopped, tmp_path) == before


async def test_late_old_session_does_not_change_effective_source_or_target(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新版已使用后旧会话晚到仍只推进旧分支，下次准备不得覆盖新版 head。

    :param stopped (RuntimeHarness): 首个旧会话
    :param tmp_path (Path): 内容卷
    """
    late = await new_session(stopped, tmp_path)
    await update_version(stopped, tmp_path, "new")
    migrated = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    await new_session(stopped, tmp_path)
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/late": b"late"}))
    resumed = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert (
        resumed.mode == "resume" and resumed.result_checkpoint_id == migrated.result_checkpoint_id
    )
    async with stopped.database() as session:
        ledger = await session.scalar(
            select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == stopped.owner)
        )
        assert ledger is not None and ledger.state_id == migrated.target_state_id


async def test_last_directory_cas_failure_rolls_back_inputs_branch_and_receipt(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    最后目录交换失败时，调用方即使提交外层事务也不会留下部分迁移。

    :param stopped (RuntimeHarness): 已使用分支
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 最后交换故障注入
    """
    await update_version(stopped, tmp_path, "new")
    payload = await request(stopped, tmp_path)
    async with stopped.database() as session:
        uploads = await session.scalar(
            select(func.count())
            .select_from(SkillContentUpload)
            .where(SkillContentUpload.user_id == stopped.owner)
        )

    async def reject(
        self: SkillPublicationRepository, directory: AccountSkillDirectoryState, checkpoint_id: UUID
    ) -> bool:
        """
        模拟最后完整目录交换遭遇竞争。

        :param directory (AccountSkillDirectoryState): 原始目录
        :param checkpoint_id (UUID): 已准备结果
        :return bool: 本次交换失败
        """
        return False

    monkeypatch.setattr(SkillPublicationRepository, "advance_directory", reject)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError, match="directory changed"):
            await SkillPreparationService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).execute(stopped.owner, payload)
    assert (await command(stopped, tmp_path)).expected == payload.expected
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillContentUpload)
                .where(SkillContentUpload.user_id == stopped.owner)
            )
            == uploads
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillBranchPreparation)
                .where(SkillBranchPreparation.user_id == stopped.owner)
            )
            == 0
        )


async def test_preview_quota_and_stale_preconditions_reject_without_mutation(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    预览检查真实存储额度，配置漂移和改变同键请求均不能悄悄执行。

    :param stopped (RuntimeHarness): 已使用分支
    :param tmp_path (Path): 内容卷
    """
    await update_version(stopped, tmp_path, "new")
    payload = await request(stopped, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await prepare(
            stopped,
            tmp_path,
            payload.model_copy(update={"dry_run": True}),
            SkillStoragePolicy(user_state_bytes=1),
        )
    assert error.value.code == "QUOTA_EXCEEDED"
    assert (await command(stopped, tmp_path)).expected == payload.expected
    await update_version(stopped, tmp_path, "newer")
    with pytest.raises(SkillContentError) as error:
        await prepare(stopped, tmp_path, payload)
    assert error.value.code == "STATE_PRECONDITION_CHANGED"
    accepted = await request(stopped, tmp_path)
    await prepare(stopped, tmp_path, accepted)
    with pytest.raises(SkillContentError) as error:
        await prepare(stopped, tmp_path, accepted.model_copy(update={"expected": payload.expected}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_original_snapshot_retry_never_rewinds_effective_history(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新版已有成功预约后，旧预约的幂等重试不能把自动迁移来源倒退。

    :param prepared (RuntimeHarness): 尚未预约的会话
    :param tmp_path (Path): 内容卷
    """
    original = await reserve(prepared, tmp_path)
    await update_version(prepared, tmp_path, "new")
    result = await prepare(prepared, tmp_path, await request(prepared, tmp_path))
    latest = await new_session(prepared, tmp_path)
    assert (await reserve(prepared, tmp_path)).id == original.id
    async with prepared.database() as session:
        ledger = await session.scalar(
            select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == prepared.owner)
        )
        assert ledger is not None and ledger.snapshot_id == latest.snapshot
        assert ledger.state_id == result.target_state_id


@pytest.mark.parametrize("missing_history", [False, True])
async def test_missing_or_expired_source_is_not_silently_reset(
    stopped: RuntimeHarness, tmp_path: Path, missing_history: bool
) -> None:
    """
    无可信使用历史或来源已过期时拒绝猜测，不从最新登记版本偷偷初始化。

    :param stopped (RuntimeHarness): 已使用旧分支
    :param tmp_path (Path): 内容卷
    :param missing_history (bool): 是否模拟旧部署缺失使用记录
    """
    from sqlalchemy import delete, update

    async with stopped.database.begin() as session:
        if missing_history:
            await session.execute(
                delete(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == stopped.owner)
            )
        else:
            await session.execute(
                update(AccountSkillState)
                .where(AccountSkillState.id == stopped.state)
                .values(expired=True)
            )
    await update_version(stopped, tmp_path, "new")
    payload = await request(stopped, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await prepare(stopped, tmp_path, payload)
    assert error.value.code == ("STATE_HISTORY_UNAVAILABLE" if missing_history else "STATE_EXPIRED")
    assert (await command(stopped, tmp_path)).expected == payload.expected


async def test_links_only_in_current_directory_still_block_isolated_migration(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    来源分支没有的当前目录反向依赖也参与关联单元，不能只检查旧输入。

    :param stopped (RuntimeHarness): 最初旧版本会话
    :param tmp_path (Path): 私有内容卷
    """
    old = (await command(stopped, tmp_path)).expected.targets[0]
    await update_version(stopped, tmp_path, "two")
    await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    newer = await new_session(stopped, tmp_path)
    await publish(
        newer, tmp_path, await ingest(newer, tmp_path, {}, links={"alias": "learning/SKILL.md"})
    )
    await pin(stopped, tmp_path, old.revision_id)
    await new_session(stopped, tmp_path)
    await update_version(stopped, tmp_path, "three")
    await pin(stopped, tmp_path, None)
    before = await directory_tree(stopped, tmp_path)
    result = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    assert result.status == "conflicted" and result.source_revision_id == old.revision_id
    assert result.conflicts[0].reason == "source_conflict"
    assert result.conflicts[0].unit == (".", "learning")
    assert await directory_tree(stopped, tmp_path) == before


async def test_resume_preview_applies_same_admission_as_commit(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    部署降低配额后，复用分支的预览和提交必须给出一致失败而不新建回执。

    :param stopped (RuntimeHarness): 已初始化的真实分支
    :param tmp_path (Path): 内容卷
    """
    payload = await request(stopped, tmp_path)
    for dry_run in (True, False):
        with pytest.raises(SkillContentError) as error:
            await prepare(
                stopped,
                tmp_path,
                payload.model_copy(update={"dry_run": dry_run}),
                SkillStoragePolicy(user_state_bytes=1),
            )
        assert error.value.code == "QUOTA_EXCEEDED"
    assert (await command(stopped, tmp_path)).expected == payload.expected


async def test_reset_without_actual_use_is_not_an_automatic_migration_source(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    只有重置但从未预约过的旧分支不算有效使用历史，新版仍从原始包初始化。

    :param prepared (RuntimeHarness): 尚未实际使用的账户
    :param tmp_path (Path): 私有内容卷
    """
    await state_execute(prepared, tmp_path, await command(prepared, tmp_path))
    await update_version(prepared, tmp_path, "new")
    result = await prepare(prepared, tmp_path, await request(prepared, tmp_path))
    assert result.mode == "initial" and result.status == "ready"
    assert result.source_checkpoint_id is None and result.source_revision_id is None
