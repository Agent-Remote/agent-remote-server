"""
验证真实发布后的目录整理、完整预览比较、等价视图、关联阻断与事务回滚。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import versions
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute

from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
)
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.compaction import SkillDirectoryCompactionService
from agent_remote_server.services.skills.compaction.plan import CompactionPlan, CompactionResult
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import history_records
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def preview(
    state: RuntimeHarness, root: Path, *identities: UUID, early: bool = True
) -> CompactionPlan:
    """
    独立只读事务提交后验证不留下任何预览写入。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param identities (UUID): 明确历史 checkpoint
    :param early (bool): 是否提前终止等待
    :return CompactionPlan: 完整原始计划
    """
    async with state.database.begin() as session:
        return await SkillDirectoryCompactionService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).preview(state.owner, state.account, tuple(identities), all_unreferenced=early)


async def apply(state: RuntimeHarness, root: Path, plan: CompactionPlan) -> CompactionResult:
    """
    以独立业务事务重建服务，发布只能依靠原计划与数据库状态。

    :param state (RuntimeHarness): 原始账户
    :param root (Path): 内容卷
    :param plan (CompactionPlan): 原始预览
    :return CompactionResult: 已提交身份交换
    """
    async with state.database.begin() as session:
        return await SkillDirectoryCompactionService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).apply(state.owner, plan)


async def shared_directory(
    state: RuntimeHarness, root: Path, *, linked: bool = False, invalid: bool = False
) -> tuple[UUID, UUID]:
    """
    两项真实发布共享同一 backing，再切换其中一项原始版本使旧 head 成为历史。

    :param state (RuntimeHarness): 原始会话
    :param root (Path): 内容卷
    :param linked (bool): 是否使保留项通过相对链接依赖待整理项
    :param invalid (bool): 是否保留运行期产生的无效 helper 说明
    :return tuple[UUID, UUID]: 历史 learning head 与仍当前 helper head
    """
    await publish(state, root, await ingest(state, root, {}))
    library = LibraryHarness(state.database, root, state.owner)
    await library.add(await library.candidate(name="helper"))
    both = await new_session(state, root)
    await publish(
        both,
        root,
        await ingest(
            both,
            root,
            {
                "learning/memory": b"historical",
                "helper/memory": b"current",
                "aux": b"keep auxiliary",
                **({"helper/SKILL.md": b"---\nname: wrong\n---\n"} if invalid else {}),
            },
            links={"helper/link": "../learning/memory"} if linked else None,
        ),
    )
    old = (await command(state, root)).expected.targets[0].head_checkpoint_id
    keep = (await command(state, root, skill="helper")).expected.targets[0].head_checkpoint_id
    assert old is not None and keep is not None
    await versions(state, root)
    return old, keep


async def fingerprint(state: RuntimeHarness) -> tuple[object, ...]:
    """
    记录全部历史时钟与关键计数，防止预览和失败事务留下上传或隐藏计量变化。

    :param state (RuntimeHarness): 已授权账户
    :return tuple[object, ...]: 可比较的持久化指纹
    """
    async with state.database() as session:
        index = await SkillRetentionRepository(session).load(state.owner)
        usage = await session.get(SkillStorageUsage, state.owner)
        assert usage is not None
        return (
            usage.lock_version,
            usage.state_bytes,
            usage.package_bytes,
            tuple(
                sorted(
                    (row.id, row.head_checkpoint_id, row.epoch, row.expired)
                    for row in index.branches
                )
            ),
            tuple(
                sorted(
                    (row.account_id, row.head_checkpoint_id, row.epoch) for row in index.directories
                )
            ),
            tuple(
                sorted(
                    (key, row.retention_released_at) for key, row in history_records(index).items()
                )
            ),
            len(index.checkpoints),
            len(index.members),
            len(index.uploads),
        )


async def test_compaction_preview_is_read_only_and_preserves_item_export(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    原子移除旧成员并重建保留 head，文件语义和纪元不变，历史时钟只从真实解除时开始。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    before = await fingerprint(stopped)
    plan = await preview(stopped, tmp_path, old)
    assert await preview(stopped, tmp_path, old) == plan
    assert await fingerprint(stopped) == before
    assert len(plan.directories) == 1 and plan.changed_directories() == {plan.directory_head_id}
    assert [row.entry_name for row in plan.directories[0].removed] == ["learning"]
    assert not plan.directories[0].blocked
    old_helper = (await command(stopped, tmp_path, skill="helper")).expected.targets[0]
    async with stopped.database() as session:
        queries = SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        exported = await queries.tree(stopped.owner, keep)
        original_head = await session.get(SkillCheckpoint, keep)
        assert original_head is not None and original_head.retention_released_at is None
    result = await apply(stopped, tmp_path, plan)
    current = (await command(stopped, tmp_path, skill="helper")).expected
    assert current.directory_epoch == plan.directory_epoch
    assert current.targets[0].state_epoch == old_helper.state_epoch
    assert current.targets[0].head_checkpoint_id == dict(result.head_replacements)[keep]
    assert not current.targets[0].expired
    assert result.directory_head_id != plan.directory_head_id
    assert {
        entry.path.split("/", 1)[0] for entry in (await directory_tree(stopped, tmp_path)).entries
    } == {"helper", "aux"}
    async with stopped.database() as session:
        queries = SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        replacement = await queries.tree(stopped.owner, dict(result.head_replacements)[keep])
        assert (
            replacement.manifest == exported.manifest
            and replacement.tree_digest == exported.tree_digest
        )
        assert replacement.source_tree_digest != exported.source_tree_digest
        old_head = await session.get(SkillCheckpoint, keep)
        old_directory = await session.get(SkillCheckpoint, plan.directory_head_id)
        assert old_head is not None and old_directory is not None
        assert old_head.retained and old_directory.retained
        assert old_head.retention_released_at is not None
        assert old_head.retention_released_at == old_directory.retention_released_at
        members = await queries.members(stopped.owner, plan.directory_head_id)
        assert {member.entry_name for member in members.items} == {"learning", "helper"}
        members = await queries.members(stopped.owner, result.directory_head_id)
        assert (
            len(members.items) == 1 and members.items[0].checkpoint_id == replacement.checkpoint_id
        )
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None and (usage.state_bytes, usage.package_bytes) == before[1:3]
        obsolete = await session.get(SkillCheckpoint, old)
        assert obsolete is not None and obsolete.retained
        branch = await session.get(AccountSkillState, obsolete.state_id)
        assert branch is not None and not branch.expired and branch.head_checkpoint_id == old
    with pytest.raises(SkillContentError) as error:
        await apply(stopped, tmp_path, plan)
    assert error.value.code == "HEAD_CHANGED"


async def test_compaction_keeps_linked_roots_and_noop_writes_no_content(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    相对链接使完整关联根继续保留，执行零变化计划不新建上传、树或 checkpoint。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path, linked=True)
    plan = await preview(stopped, tmp_path, old)
    assert not plan.changed_directories()
    assert [member.entry_name for member in plan.directories[0].blocked] == ["learning"]
    assert plan.directories[0].result == plan.directories[0].original
    before = await fingerprint(stopped)
    result = await apply(stopped, tmp_path, plan)
    assert not result.directory_replacements and not result.head_replacements
    after = await fingerprint(stopped)
    assert after[1:] == before[1:]


async def test_compaction_requires_exact_history_deadline_and_current_protection(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    普通历史等真实期限，当前停用项和 pin 仍阻断整理，提前标记不越过保护。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await preview(stopped, tmp_path, old, early=False)
    assert error.value.code == "HISTORY_NOT_EXPIRED"
    async with stopped.database.begin() as session:
        history = await session.get(SkillCheckpoint, old)
        assert history is not None
        history.retention_released_at = datetime.now(UTC) - timedelta(days=31)
        branch = await session.get(AccountSkillState, history.state_id)
        assert branch is not None and branch.base_revision_id is not None
        revision = branch.base_revision_id
    plan = await preview(stopped, tmp_path, old, early=False)
    assert plan.changed_directories()
    with pytest.raises(SkillContentError) as error:
        await preview(stopped, tmp_path, keep)
    assert error.value.code == "STATE_PROTECTED"
    await pin(stopped, tmp_path, revision)
    before = await fingerprint(stopped)
    with pytest.raises(SkillContentError) as error:
        await apply(stopped, tmp_path, plan)
    assert error.value.code == "STATE_PROTECTED"
    assert await fingerprint(stopped) == before


async def test_compaction_failure_rolls_back_every_new_reference(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    最后目录 CAS 失败和外层回滚都撤销此前新树、成员、head 与时钟；捕获后提交也安全。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入最后 CAS 失败
    """
    from agent_remote_server.models.skill_state import AccountSkillDirectoryState
    from agent_remote_server.repositories.skill_publication import SkillPublicationRepository

    old, _ = await shared_directory(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    before = await fingerprint(stopped)

    async def reject(
        self: SkillPublicationRepository, directory: AccountSkillDirectoryState, checkpoint_id: UUID
    ) -> bool:
        """
        模拟所有新内容完成后的最终比较失败。

        :param directory (AccountSkillDirectoryState): 当前权威
        :param checkpoint_id (UUID): 尚未提交的新 head
        :return bool: 测试注入失败
        """
        return False

    with monkeypatch.context() as patch:
        patch.setattr(SkillPublicationRepository, "advance_directory", reject)
        async with stopped.database.begin() as session:
            with pytest.raises(SkillContentError) as error:
                await SkillDirectoryCompactionService(
                    session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
                ).apply(stopped.owner, plan)
            assert error.value.code == "HEAD_CHANGED"
    assert (await fingerprint(stopped))[1:] == before[1:]
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with stopped.database.begin() as session:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply(stopped.owner, plan)
            raise RuntimeError("outer rollback")
    assert (await fingerprint(stopped))[1:] == before[1:]


async def test_compaction_detects_changed_directory_and_account_before_publishing(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新目录发布后旧确认失效，另一用户和混合非法 checkpoint 不获得任何写入。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    with pytest.raises(SkillContentError) as error:
        await preview(stopped, tmp_path, old, uuid4())
    assert error.value.code == "CHECKPOINT_NOT_FOUND"
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDirectoryCompactionService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            ).apply(uuid4(), plan)
        assert error.value.code == "ACCOUNT_NOT_FOUND"
    await execute(stopped, tmp_path, await command(stopped, tmp_path, skill="helper"))
    before = await fingerprint(stopped)
    with pytest.raises(SkillContentError) as error:
        await apply(stopped, tmp_path, plan)
    assert error.value.code == "HEAD_CHANGED"
    assert (await fingerprint(stopped))[1:] == before[1:]


async def test_compaction_propagates_backing_replacement_without_rewriting_unselected_root(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    当前目录与 helper backing 不同，移除 backing 中旧版根只改当前成员指针，不删除同名未选内容。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, keep = await shared_directory(stopped, tmp_path)
    async with stopped.database() as session:
        checkpoint = await session.get(SkillCheckpoint, old)
        assert checkpoint is not None
        branch = await session.get(AccountSkillState, checkpoint.state_id)
        assert branch is not None and branch.base_revision_id is not None
        revision = branch.base_revision_id
    await pin(stopped, tmp_path, revision)
    late = await new_session(stopped, tmp_path)
    await publish(
        late,
        tmp_path,
        await ingest(
            late,
            tmp_path,
            {"learning/latest": b"unselected current directory content", "aux": b"new auxiliary"},
        ),
    )
    await pin(stopped, tmp_path, None)
    from test_skill_compaction_projection import mapped_protection, projected

    original_current = await directory_tree(stopped, tmp_path)
    plan = await preview(stopped, tmp_path, old)
    assert len(plan.directories) == 2 and len(plan.changed_directories()) == 2
    current_plan = next(
        row for row in plan.directories if row.checkpoint_id == plan.directory_head_id
    )
    assert current_plan.original == current_plan.result and not current_plan.removed
    backing_plan = next(
        row for row in plan.directories if row.checkpoint_id != plan.directory_head_id
    )
    assert [member.checkpoint_id for member in backing_plan.removed] == [old]
    predicted = await projected(stopped, tmp_path, plan)
    result = await apply(stopped, tmp_path, plan)
    assert len(result.directory_replacements) == 2 and len(result.head_replacements) == 1
    assert await directory_tree(stopped, tmp_path) == original_current
    async with stopped.database() as session:
        from agent_remote_server.services.skills.retention import SkillRetentionInspector

        protected = await SkillRetentionInspector(session).inspect(stopped.owner)
        assert mapped_protection(predicted, result) == protected
        replacement = await session.get(SkillCheckpoint, dict(result.head_replacements)[keep])
        assert replacement is not None
        assert (
            replacement.backing_directory_id
            == dict(result.directory_replacements)[backing_plan.checkpoint_id]
        )
        queries = SkillStateQueryService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        members = await queries.members(stopped.owner, result.directory_head_id)
        assert (
            next(member for member in members.items if member.entry_name == "helper").checkpoint_id
            == replacement.id
        )
        assert (
            next(
                member for member in members.items if member.entry_name == "learning"
            ).checkpoint_id
            != old
        )
        assert (await queries.tree(stopped.owner, keep)).manifest == (
            await queries.tree(stopped.owner, replacement.id)
        ).manifest


async def test_compaction_keeps_disabled_current_branch_and_original_snapshot_identity(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    停用不解除当前 head，等价替换保持选定版本和规则，独立精确快照仍使用原始 checkpoint。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope

    old, keep = await shared_directory(stopped, tmp_path)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="helper",
            scope=SkillScope(account_id=stopped.account),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    disabled = (await command(stopped, tmp_path, skill="helper")).expected.targets[0]
    assert not disabled.rule.enabled
    plan = await preview(stopped, tmp_path, old)
    assert any(head.checkpoint_id == keep for head in plan.heads)
    with pytest.raises(SkillContentError) as error:
        await preview(stopped, tmp_path, keep)
    assert error.value.code == "STATE_PROTECTED"
    result = await apply(stopped, tmp_path, plan)
    after = (await command(stopped, tmp_path, skill="helper")).expected.targets[0]
    assert after.rule == disabled.rule and after.revision_id == disabled.revision_id
    assert after.head_checkpoint_id == dict(result.head_replacements)[keep]
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        snapshot_members = [row.checkpoint_id for row in index.snapshot_items]
        assert snapshot_members
        assert all(identity not in snapshot_members for _, identity in result.head_replacements)
        originals = {row.id: row for row in index.checkpoints}
        assert all(originals[identity].retained for identity in snapshot_members)


async def test_compaction_preview_rejects_missing_bytes_and_changed_quota_without_writes(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    清单存在不等于内容完整；收紧配额也要在预览拒绝，而不是等发布写入一半才发现。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    before = await fingerprint(stopped)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillDirectoryCompactionService(
                session,
                PrivateObjectStore(tmp_path / "objects"),
                SkillStoragePolicy(user_state_bytes=1),
            ).preview(stopped.owner, stopped.account, (old,), all_unreferenced=True)
        assert error.value.code == "QUOTA_EXCEEDED"
    assert await fingerprint(stopped) == before
    tree = await directory_tree(stopped, tmp_path)
    entry = next(entry for entry in tree.entries if entry.path == "helper/memory")
    path = tmp_path / "objects" / str(stopped.owner) / entry.sha256[:2] / entry.sha256
    original = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(SkillContentError) as error:
            await preview(stopped, tmp_path, old)
        assert error.value.code == "CONTENT_INCOMPLETE"
        assert await fingerprint(stopped) == before
    finally:
        path.write_bytes(original)
        path.chmod(0o400)
