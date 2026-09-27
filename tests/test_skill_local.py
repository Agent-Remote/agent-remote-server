"""
验证账户本地候选隔离、完整目录引用及快照暴露边界。
"""

import asyncio
import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_content_service import service
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve
from test_skill_storage import file_entry

from agent_remote_server.models.skill_local import AccountLocalSkill, AccountLocalSkillRevision
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshotItem
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.local_candidates import LocalSkillCandidateService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def source(
    state: RuntimeHarness,
    root: Path,
    name: str = "notes",
    *,
    linked: bool = False,
    content: bytes = b"# Instructions\n",
) -> SkillCheckpoint:
    """
    上传真实完整目录并建立候选来源，不变更账户目录 head。

    :param state (RuntimeHarness): 已授权测试账户
    :param root (Path): 内容卷
    :param name (str): 候选目录名
    :param linked (bool): 说明是否链接到根级辅助文件
    :param content (bytes): 实际说明字节
    :return SkillCheckpoint: 已提交的完整来源目录
    """
    document = file_entry(content, path="shared.md" if linked else name + "/SKILL.md")
    entries = [SkillTreeEntry(path=name, kind="directory", mode=0o755), document]
    if linked:
        entries.append(
            SkillTreeEntry(
                path=name + "/SKILL.md", kind="symlink", mode=0o777, target="../shared.md"
            )
        )
    manifest = SkillTreeManifest(entries=tuple(sorted(entries, key=lambda entry: entry.path)))
    async with state.database.begin() as session:
        content_service = service(session, root)
        upload = await content_service.begin(
            state.owner, str(uuid4()), manifest, "account_directory"
        )
        await content_service.put_file(state.owner, upload.id, document.sha256, io.BytesIO(content))
        tree = await content_service.complete(state.owner, upload.id)
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=state.owner,
            account_id=state.account,
            scope="directory",
            content_digest=tree.digest,
            tree_digest=tree.digest,
        )
        session.add(checkpoint)
    return checkpoint


async def register(
    state: RuntimeHarness,
    root: Path,
    checkpoint: SkillCheckpoint,
    name: str = "notes",
    *,
    account: UUID | None = None,
) -> AccountLocalSkill:
    """
    在独立事务中调用候选登记服务。

    :param state (RuntimeHarness): 请求用户身份
    :param root (Path): 内容卷
    :param checkpoint (SkillCheckpoint): 源检查点
    :param name (str): 候选名称
    :param account (UUID | None): 可选替换目标账户
    :return AccountLocalSkill: 已提交的候选
    """
    async with state.database.begin() as session:
        candidates = LocalSkillCandidateService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        return await candidates.register(state.owner, account or state.account, checkpoint.id, name)


async def activate(state: RuntimeHarness, item: AccountLocalSkill, *, enabled: bool = True) -> None:
    """
    模拟未来发布事务已完成激活，避免为候选登记引入隐式发布。

    :param state (RuntimeHarness): 测试账户
    :param item (AccountLocalSkill): 已保存候选
    :param enabled (bool): 账户启用值
    """
    async with state.database.begin() as session:
        current = await session.get(AccountLocalSkill, item.id)
        assert current is not None
        current.status = "active"
        current.enabled = enabled


async def test_candidate_retry_preserves_identity_without_publishing(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    相同源重试复用身份，同摘要不同源仍独立，且不进入用户库或快照。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    checkpoint = await source(prepared, tmp_path, linked=True)
    item = await register(prepared, tmp_path, checkpoint)
    repeated = await register(prepared, tmp_path, checkpoint)
    other = await register(prepared, tmp_path, await source(prepared, tmp_path, linked=True))
    assert item.id == repeated.id and other.id != item.id
    async with prepared.database() as session:
        revision = await session.get(AccountLocalSkillRevision, item.default_revision_id)
        assert revision is not None and revision.tree_digest == checkpoint.tree_digest
        assert revision.subtree_prefix == "notes" and revision.metadata_json["name"] == "notes"
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None and directory.head_checkpoint_id == prepared.directory
        assert not await SkillLocalRepository(session).active(prepared.owner, prepared.account)
        library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
        assert (await library.info()).name == "learning"
    snapshot = await reserve(prepared, tmp_path)
    async with prepared.database() as session:
        entries = (
            await session.scalars(
                select(SessionSkillSnapshotItem).where(
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id
                )
            )
        ).all()
        assert [entry.entry_name for entry in entries] == ["learning"]


@pytest.mark.parametrize("enabled", [True, False])
async def test_snapshot_selects_only_enabled_active_local_branch(
    prepared: RuntimeHarness,
    tmp_path: Path,
    enabled: bool,
) -> None:
    """
    本地分支有独立来源和账户规则，停用项不会初始化或暴露。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    :param enabled (bool): 账户启用值
    """
    item = await register(prepared, tmp_path, await source(prepared, tmp_path))
    assert item.default_revision_id is not None
    await activate(prepared, item, enabled=enabled)
    snapshot = await reserve(prepared, tmp_path)
    async with prepared.database() as session:
        entries = (
            await session.scalars(
                select(SessionSkillSnapshotItem).where(
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id,
                    SessionSkillSnapshotItem.entry_name == "notes",
                )
            )
        ).all()
        branch = await SkillLocalRepository(session).branch(
            prepared.owner, prepared.account, item.id, item.default_revision_id
        )
        assert bool(entries) == enabled and (branch is not None) == enabled
        if enabled:
            assert branch is not None and branch.installation_id is None
            assert branch.base_revision_id is None and branch.installation_epoch == 1
            assert entries[0].resolution_json["revision_source"] == "account"
            assert entries[0].resolution_json["revision_id"] == str(item.default_revision_id)
            assert branch.head_checkpoint_id == entries[0].checkpoint_id


async def test_local_and_library_same_name_is_an_explicit_source_conflict(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    即便名称相同，也不能把本地来源接到库来源已有分支。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    item = await register(
        prepared, tmp_path, await source(prepared, tmp_path, "learning"), "learning"
    )
    assert item.default_revision_id is not None
    await activate(prepared, item)
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path)
    assert error.value.code == "SKILL_SOURCE_CONFLICT"
    async with prepared.database() as session:
        assert not await SkillLocalRepository(session).branch(
            prepared.owner, prepared.account, item.id, item.default_revision_id
        )


@pytest.mark.parametrize("name", ["ego-browser", "agent-remote-device", "../escape", "Notes", ""])
async def test_candidate_rejects_invalid_names(
    prepared: RuntimeHarness,
    tmp_path: Path,
    name: str,
) -> None:
    """
    所有候选入口共享安装名称规则，拒绝系统及路径替换。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    :param name (str): 非法候选名称
    """
    checkpoint = await source(prepared, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await register(prepared, tmp_path, checkpoint, name)
    assert error.value.code == "INVALID_SKILL_NAME"


async def test_candidate_rejects_invalid_metadata_and_retired_checkpoint(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    真实格式错误和已退役树都不能创建本地身份。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    checkpoint = await source(prepared, tmp_path, content=b"---\ninvalid frontmatter")
    with pytest.raises(SkillContentError) as error:
        await register(prepared, tmp_path, checkpoint)
    assert error.value.code == "INVALID_SKILL_FORMAT"
    async with prepared.database.begin() as session:
        row = await session.get(SkillCheckpoint, checkpoint.id)
        assert row is not None
        row.retained = False
        row.tree_digest = None
    with pytest.raises(SkillContentError) as error:
        await register(prepared, tmp_path, checkpoint)
    assert error.value.code == "STATE_EXPIRED"


async def test_local_scope_cannot_cross_accounts(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    同一用户另一账户既不能冒用源检查点，也不能选择本地版本和分支。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    checkpoint = await source(prepared, tmp_path)
    item = await register(prepared, tmp_path, checkpoint)
    assert item.default_revision_id is not None
    await activate(prepared, item)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    other = await library.account()
    with pytest.raises(SkillContentError) as error:
        await register(prepared, tmp_path, checkpoint, account=other)
    assert error.value.code == "CHECKPOINT_NOT_FOUND"
    async with prepared.database() as session:
        local = SkillLocalRepository(session)
        assert not await local.active(prepared.owner, other)
        assert not await local.revision(prepared.owner, other, item.id, item.default_revision_id)
        assert not await local.active(uuid4(), prepared.account)
    with pytest.raises(IntegrityError):
        async with prepared.database.begin() as session:
            session.add(
                AccountSkillDirectoryState(
                    user_id=prepared.owner, account_id=other, tool_type="claude"
                )
            )
            await session.flush()
            session.add(
                AccountSkillState(
                    user_id=prepared.owner,
                    account_id=other,
                    local_skill_id=item.id,
                    local_revision_id=item.default_revision_id,
                    installation_epoch=1,
                )
            )


async def test_local_active_names_and_exclusive_branch_sources_are_database_constraints(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    活跃同名来源以及混用库和本地来源均被真实外键与检查约束拒绝。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    first = await register(prepared, tmp_path, await source(prepared, tmp_path))
    second = await register(prepared, tmp_path, await source(prepared, tmp_path))
    await activate(prepared, first)
    with pytest.raises(IntegrityError):
        await activate(prepared, second)
    with pytest.raises(IntegrityError):
        async with prepared.database.begin() as session:
            branch = await session.get(AccountSkillState, prepared.state)
            assert branch is not None
            branch.local_skill_id = first.id
            branch.local_revision_id = first.default_revision_id
    with pytest.raises(IntegrityError):
        async with prepared.database.begin() as session:
            row = await session.get(AccountLocalSkill, second.id)
            assert row is not None
            row.default_revision_id = first.default_revision_id


async def test_local_expired_branch_cannot_silently_initialize_again(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    已过期的本地运行分支必须显式恢复，不能因原始版本仍保留而重置。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    item = await register(prepared, tmp_path, await source(prepared, tmp_path))
    assert item.default_revision_id is not None
    await activate(prepared, item)
    async with prepared.database.begin() as session:
        session.add(
            AccountSkillState(
                user_id=prepared.owner,
                account_id=prepared.account,
                local_skill_id=item.id,
                local_revision_id=item.default_revision_id,
                installation_epoch=1,
                expired=True,
            )
        )
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path)
    assert error.value.code == "STATE_EXPIRED"


async def test_local_cross_entry_link_requires_current_directory_dependency(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    本地初始完整树保留链接，快照仍按当前目录依赖验证而不偷带历史根数据。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    checkpoint = await source(prepared, tmp_path, linked=True)
    item = await register(prepared, tmp_path, checkpoint)
    assert item.default_revision_id is not None
    await activate(prepared, item)
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path)
    assert error.value.code == "STATE_DEPENDENCY_MISSING"
    async with prepared.database.begin() as session:
        branch = AccountSkillState(
            id=uuid4(),
            user_id=prepared.owner,
            account_id=prepared.account,
            local_skill_id=item.id,
            local_revision_id=item.default_revision_id,
            installation_epoch=1,
        )
        session.add(branch)
        await session.flush()
        view = SkillCheckpoint(
            id=uuid4(),
            user_id=prepared.owner,
            account_id=prepared.account,
            scope="item",
            state_id=branch.id,
            subtree_prefix="notes",
            content_digest=checkpoint.content_digest,
            tree_digest=checkpoint.tree_digest,
        )
        session.add(view)
        await session.flush()
        branch.head_checkpoint_id = view.id
        session.add(
            SkillDirectoryMember(
                user_id=prepared.owner,
                account_id=prepared.account,
                directory_checkpoint_id=checkpoint.id,
                entry_name="notes",
                state_id=branch.id,
                checkpoint_id=view.id,
            )
        )
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory.head_checkpoint_id = checkpoint.id
    snapshot = await reserve(prepared, tmp_path)
    async with prepared.database() as session:
        tree = await service(session, tmp_path).read_tree(
            prepared.owner, "state", snapshot.tree_digest
        )
        links = [entry for entry in tree.entries if entry.kind == "symlink"]
        assert len(links) == 1 and links[0].target == "../shared.md"
        assert any(entry.path == "shared.md" for entry in tree.entries)


async def test_concurrent_registration_reuses_one_candidate(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    独立事务同时登记同一来源时，只生成一份稳定本地身份和版本。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    checkpoint = await source(prepared, tmp_path)
    first, second = await asyncio.gather(
        register(prepared, tmp_path, checkpoint), register(prepared, tmp_path, checkpoint)
    )
    assert first.id == second.id and first.default_revision_id == second.default_revision_id
    async with prepared.database() as session:
        revisions = (
            await session.scalars(
                select(AccountLocalSkillRevision).where(
                    AccountLocalSkillRevision.local_skill_id == first.id
                )
            )
        ).all()
        assert len(revisions) == 1
