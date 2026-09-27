"""
验证重置恢复对规则、精确分支、旧会话和完整目录的原子边界。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import baseline, directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_resolution_service import choose, pending
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
)
from agent_remote_server.models.skill_state_operations import SkillStateOperation
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.repositories.skill_state_operations import SkillStateOperationRepository
from agent_remote_server.schemas.skill_library import SkillRemoveRequest, SkillUpdateRequest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.schemas.skill_state_commands import (
    SkillStateCommand,
    SkillStateCommandView,
    SkillStateSelector,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_commands import SkillStateCommandService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def command(
    state: RuntimeHarness,
    root: Path,
    *,
    checkpoint: UUID | None = None,
    directory: bool = False,
    skill: str = "learning",
    key: str | None = None,
    dry_run: bool = False,
) -> SkillStateCommand:
    """
    在独立只读请求中取得当前精确前置条件，供真实命令比较。

    :param state (RuntimeHarness): 原始账户身份
    :param root (Path): 内容卷
    :param checkpoint (UUID | None): 可选恢复来源
    :param directory (bool): 是否完整目录范围
    :param skill (str): 单项来源
    :param key (str | None): 命令幂等键
    :param dry_run (bool): 是否仅预览
    :return SkillStateCommand: 明确预览或执行请求
    """
    async with state.database.begin() as session:
        service = SkillStateCommandService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        selector = SkillStateSelector(
            account_id=state.account,
            scope="account-directory" if directory else "item",
            skill=None if directory else skill,
        )
        selected = await service.selection.current(state.owner, selector)
        return SkillStateCommand(
            idempotency_key=key or str(uuid4()),
            action="restore" if checkpoint else "reset",
            selector=selector,
            expected=selected.precondition,
            checkpoint_id=checkpoint,
            dry_run=dry_run,
        )


async def execute(
    state: RuntimeHarness, root: Path, request: SkillStateCommand
) -> SkillStateCommandView:
    """
    在独立事务中执行并提交完整状态命令。

    :param state (RuntimeHarness): 原始账户身份
    :param root (Path): 内容卷
    :param request (SkillStateCommand): 已固定前置条件
    :return SkillStateCommandView: 提交后可观察结果
    """
    async with state.database.begin() as session:
        return await SkillStateCommandService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).execute(state.owner, request)


async def test_reset_preview_replay_restore_and_late_input_detachment(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    预览无写入，重置保留历史，恢复推进纪元，旧会话不能复活已清空内容。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    late = await new_session(stopped, tmp_path)
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/memory": b"learned", "aux": b"keep"}),
    )
    request = await command(stopped, tmp_path)
    original = request.expected.targets[0].head_checkpoint_id
    async with stopped.database() as session:
        uploads_before = await session.scalar(select(func.count()).select_from(SkillContentUpload))
    preview = await execute(stopped, tmp_path, request.model_copy(update={"dry_run": True}))
    assert preview.status == "preview" and preview.operation_id is None
    assert [entry.path for entry in preview.changes] == ["learning/memory"]
    async with stopped.database() as session:
        assert (
            await session.scalar(select(func.count()).select_from(SkillContentUpload))
            == uploads_before
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillStateOperation)
                .where(SkillStateOperation.user_id == stopped.owner)
            )
            == 0
        )
    first, replay = await asyncio.gather(
        execute(stopped, tmp_path, request), execute(stopped, tmp_path, request)
    )
    assert first == replay and first.status == "published"
    current = await command(stopped, tmp_path)
    assert request.expected.targets[0].state_epoch is not None
    assert current.expected.targets[0].state_epoch == request.expected.targets[0].state_epoch + 1
    assert current.expected.directory_epoch == request.expected.directory_epoch
    assert {entry.path for entry in (await directory_tree(stopped, tmp_path)).entries} == {
        "learning",
        "learning/SKILL.md",
        "aux",
    }
    assert original is not None
    restored = await execute(
        stopped, tmp_path, await command(stopped, tmp_path, checkpoint=original)
    )
    assert restored.status == "published"
    assert "learning/memory" in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }
    detached = await publish(
        late, tmp_path, await ingest(late, tmp_path, {"learning/late": b"old"})
    )
    assert detached.status == "detached" and detached.reason == "state_epoch_changed"
    assert await execute(stopped, tmp_path, request) == first
    with pytest.raises(SkillContentError) as error:
        await execute(
            stopped,
            tmp_path,
            request.model_copy(update={"action": "restore", "checkpoint_id": original}),
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_reset_supersedes_old_plan_without_executing_it(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    重置标记旧冲突失效，稍后的 resolve 只重算归档而不应用保存的选择。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    conflict = await pending(stopped, tmp_path)
    await choose(
        stopped, tmp_path, conflict, SkillResolutionChoice(path="learning/one", use="incoming")
    )
    reset = await execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert reset.superseded_conflicts == 1
    before = await directory_tree(stopped, tmp_path)
    async with stopped.database() as session:
        old = await session.get(SkillPublication, conflict.id)
        assert old is not None and old.status == "superseded" and old.reason == "state_reset"
    result = await choose(
        stopped,
        tmp_path,
        conflict,
        SkillResolutionChoice(path="learning/two", use="incoming"),
        revision=1,
    )
    assert result.status == "superseded" and result.replacement_id is not None
    async with stopped.database() as session:
        replacement = await session.get(SkillPublication, result.replacement_id)
        assert replacement is not None and replacement.status == "detached"
    assert await directory_tree(stopped, tmp_path) == before


async def test_directory_reset_restores_local_initial_and_preserves_disabled_members(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    目录重置清空辅助数据并恢复启用本地初始树，停用成员及其分支不变。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(
            stopped,
            tmp_path,
            {"notes/SKILL.md": b"initial", "hidden/SKILL.md": b"hidden initial", "aux": b"data"},
        ),
    )
    later = await new_session(stopped, tmp_path)
    await publish(
        later,
        tmp_path,
        await ingest(
            later,
            tmp_path,
            {
                "notes/SKILL.md": b"modified",
                "hidden/memory": b"retained",
                "learning/memory": b"learned",
            },
        ),
    )
    async with stopped.database.begin() as session:
        hidden = await session.scalar(
            select(AccountLocalSkill).where(
                AccountLocalSkill.account_id == stopped.account, AccountLocalSkill.name == "hidden"
            )
        )
        assert hidden is not None
        hidden.enabled = False
        branch = await session.scalar(
            select(AccountSkillState).where(AccountSkillState.local_skill_id == hidden.id)
        )
        assert branch is not None
        hidden_id, hidden_head, hidden_epoch = branch.id, branch.head_checkpoint_id, branch.epoch
    request = await command(stopped, tmp_path, directory=True)
    assert {target.name for target in request.expected.targets} == {"learning", "notes"}
    result = await execute(stopped, tmp_path, request)
    assert result.directory_epoch_advances
    tree = await directory_tree(stopped, tmp_path)
    assert {entry.path for entry in tree.entries} == {
        "learning",
        "learning/SKILL.md",
        "notes",
        "notes/SKILL.md",
        "hidden",
        "hidden/SKILL.md",
        "hidden/memory",
    }
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, hidden_id)
        assert branch is not None and (branch.head_checkpoint_id, branch.epoch) == (
            hidden_head,
            hidden_epoch,
        )
    next_command = await command(stopped, tmp_path, directory=True)
    assert request.expected.directory_epoch is not None
    assert next_command.expected.directory_epoch == request.expected.directory_epoch + 1


async def test_new_revision_can_reset_but_cannot_restore_old_revision(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    显式重置可为待迁移版本建立干净分支，恢复不能把旧版本文件灌入新版。

    :param stopped (RuntimeHarness): 旧版本会话
    :param tmp_path (Path): 内容卷
    """
    old = (await command(stopped, tmp_path)).expected.targets[0]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    request = await command(stopped, tmp_path)
    assert request.expected.targets[0].state_id is None
    assert request.expected.targets[0].revision_id != old.revision_id
    assert old.head_checkpoint_id is not None
    with pytest.raises(SkillContentError) as error:
        await execute(
            stopped, tmp_path, await command(stopped, tmp_path, checkpoint=old.head_checkpoint_id)
        )
    assert error.value.code == "STATE_SCOPE_MISMATCH"
    await execute(stopped, tmp_path, request)
    current = (await command(stopped, tmp_path)).expected.targets[0]
    assert current.state_id is not None and current.state_id != old.state_id
    async with stopped.database() as session:
        original = await session.get(AccountSkillState, old.state_id)
        assert (
            original is not None
            and original.head_checkpoint_id == old.head_checkpoint_id
            and original.epoch == old.state_epoch
        )
    assert (await baseline(await new_session(stopped, tmp_path), tmp_path)).entries


async def test_directory_restore_checks_full_identity_set_and_restores_auxiliary(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    目录恢复恢复辅助数据，但来源集合发生变化后不能隐式启用或删除来源。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    original = await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/memory": b"learned", "aux": b"original"}),
    )
    assert original.result_checkpoint_id is not None
    await execute(stopped, tmp_path, await command(stopped, tmp_path, directory=True))
    restored = await execute(
        stopped,
        tmp_path,
        await command(stopped, tmp_path, directory=True, checkpoint=original.result_checkpoint_id),
    )
    assert restored.status == "published" and "aux" in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }
    later = await new_session(stopped, tmp_path)
    await publish(later, tmp_path, await ingest(later, tmp_path, {"notes/SKILL.md": b"new source"}))
    before = await directory_tree(stopped, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await execute(
            stopped,
            tmp_path,
            await command(
                stopped, tmp_path, directory=True, checkpoint=original.result_checkpoint_id
            ),
        )
    assert error.value.code == "STATE_SCOPE_MISMATCH" and error.value.details
    assert await directory_tree(stopped, tmp_path) == before


async def test_same_source_reinstall_allows_explicit_restore(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同来源重装产生新安装纪元，用户可明确恢复同原始版本的旧历史。

    :param stopped (RuntimeHarness): 旧安装会话
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"old learning"})
    )
    old = (await command(stopped, tmp_path)).expected.targets[0]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    await library.add(await library.candidate())
    request = await command(stopped, tmp_path, checkpoint=old.head_checkpoint_id)
    assert request.expected.targets[0].installation_epoch == old.installation_epoch + 1
    await execute(stopped, tmp_path, request)
    assert "learning/memory" in {
        entry.path for entry in (await directory_tree(stopped, tmp_path)).entries
    }


async def test_concurrent_changed_precondition_and_late_cas_rollback(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    并发旧预览只有一次成功，最终目录 CAS 失败回滚全部分支纪元和内容引用。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 控制最后 CAS 失败
    """
    first = await command(stopped, tmp_path)
    second = first.model_copy(update={"idempotency_key": str(uuid4())})
    results = await asyncio.gather(
        execute(stopped, tmp_path, first),
        execute(stopped, tmp_path, second),
        return_exceptions=True,
    )
    assert sum(isinstance(item, SkillStateCommandView) for item in results) == 1
    failure = next(item for item in results if isinstance(item, SkillContentError))
    assert failure.code == "STATE_PRECONDITION_CHANGED"

    async def reject(
        self: SkillStateOperationRepository,
        directory: AccountSkillDirectoryState,
        checkpoint_id: UUID,
        advance_epoch: bool,
    ) -> bool:
        """
        模拟目录在最后比较交换时不再匹配。

        :param directory (AccountSkillDirectoryState): 旧目录
        :param checkpoint_id (UUID): 新检查点
        :param advance_epoch (bool): 目录纪元标志
        :return bool: 模拟未命中
        """
        return False

    request = await command(stopped, tmp_path, directory=True)
    monkeypatch.setattr(SkillStateOperationRepository, "advance_directory", reject)
    with pytest.raises(SkillContentError, match="directory changed"):
        await execute(stopped, tmp_path, request)
    assert (await command(stopped, tmp_path, directory=True)).expected == request.expected
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(SkillStateOperation).where(
                    SkillStateOperation.idempotency_key == request.idempotency_key
                )
            )
            is None
        )


async def test_directory_reset_rejects_missing_initial_link_dependency(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    本地初始技能依赖根数据时，清空辅助数据的目录重置必须整体失败。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(
            stopped,
            tmp_path,
            {"notes/SKILL.md": b"local", "aux": b"needed"},
            links={"notes/link": "../aux"},
        ),
    )
    request = await command(stopped, tmp_path, directory=True, dry_run=True)
    with pytest.raises(SkillContentError) as error:
        await execute(stopped, tmp_path, request)
    assert error.value.code == "STATE_DEPENDENCY_MISSING"
    assert (await command(stopped, tmp_path, directory=True)).expected == request.expected


async def test_expired_branch_requires_explicit_reset_and_preview_checks_storage_quota(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已过期分支可明确重置，低配额预览与实际提交遵守同一限制。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    async with stopped.database.begin() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        branch.expired = True
    request = await command(stopped, tmp_path, dry_run=True)
    assert request.expected.targets[0].expired
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillStateCommandService(
                session,
                PrivateObjectStore(tmp_path / "objects"),
                SkillStoragePolicy(user_state_bytes=1),
            ).execute(stopped.owner, request)
        assert error.value.code == "QUOTA_EXCEEDED"
    assert (await command(stopped, tmp_path)).expected == request.expected
    await execute(stopped, tmp_path, request.model_copy(update={"dry_run": False}))
    assert not (await command(stopped, tmp_path)).expected.targets[0].expired


async def test_restore_rejects_other_account_and_expired_checkpoint(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同用户也不能跨账户恢复目录，过期墓碑不能被恢复为空状态。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    other_account = await library.account()
    foreign, expired = uuid4(), uuid4()
    async with stopped.database.begin() as session:
        session.add(
            AccountSkillDirectoryState(
                user_id=stopped.owner, account_id=other_account, tool_type="claude"
            )
        )
        await session.flush()
        session.add(
            SkillCheckpoint(
                id=foreign,
                user_id=stopped.owner,
                account_id=other_account,
                scope="directory",
                content_digest=stopped.tree,
                tree_digest=stopped.tree,
            )
        )
        session.add(
            SkillCheckpoint(
                id=expired,
                user_id=stopped.owner,
                account_id=stopped.account,
                scope="item",
                state_id=stopped.state,
                subtree_prefix="learning",
                content_digest=stopped.tree,
                retained=False,
            )
        )
    for identity, directory, code in (
        (foreign, True, "STATE_SCOPE_MISMATCH"),
        (expired, False, "STATE_EXPIRED"),
    ):
        request = await command(stopped, tmp_path, directory=directory, checkpoint=identity)
        with pytest.raises(SkillContentError) as error:
            await execute(stopped, tmp_path, request)
        assert error.value.code == code
        assert (await command(stopped, tmp_path, directory=directory)).expected == request.expected


@pytest.mark.parametrize("substitution", ["owner", "source_scope", "result_scope"])
async def test_state_operation_foreign_keys_reject_substituted_recovery_references(
    stopped: RuntimeHarness, tmp_path: Path, substitution: str
) -> None:
    """
    数据库层拒绝跨所有者或错误对象范围的恢复输入和结果引用。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param substitution (str): 被替换的归属或范围
    """
    original = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    assert original is not None
    result = await execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert result.result_checkpoint_id is not None
    owner = await user(stopped.database) if substitution == "owner" else stopped.owner
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            session.add(
                SkillStateOperation(
                    user_id=owner,
                    account_id=stopped.account,
                    idempotency_key=str(uuid4()),
                    request_digest="a" * 64,
                    action="restore",
                    scope="directory" if substitution == "source_scope" else "item",
                    source_checkpoint_id=original,
                    result_checkpoint_id=original
                    if substitution == "result_scope"
                    else result.result_checkpoint_id,
                    response_json={},
                )
            )
            await session.flush()
    async with stopped.database() as session:
        assert await session.get(SkillStateOperation, result.operation_id) is not None
