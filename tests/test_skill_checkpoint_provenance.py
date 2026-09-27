"""
验证检查点保存真实历史纪元和完整目录来源，不能被相同摘要或当前状态替代。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness, runtime
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_local import activate, register, source
from test_skill_migration import migrate, request, versions
from test_skill_preparation import prepare
from test_skill_preparation import request as preparation_request
from test_skill_resolution_service import choose, pending
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice


async def checkpoint(state: RuntimeHarness, identity: UUID) -> SkillCheckpoint:
    """
    独立事务重读原记录，避免通过缓存状态证明历史不变。

    :param state (RuntimeHarness): 原账户
    :param identity (UUID): 精确检查点身份
    :return SkillCheckpoint: 已保存原记录
    """
    async with state.database() as session:
        row = await session.get(SkillCheckpoint, identity)
        assert row is not None
        return row


async def member(
    state: RuntimeHarness, directory_id: UUID, name: str = "learning"
) -> SkillCheckpoint:
    """
    通过真实目录成员引用取得单项，不通过树摘要猜测所属目录。

    :param state (RuntimeHarness): 原账户
    :param directory_id (UUID): 完整目录身份
    :param name (str): 确切来源根
    :return SkillCheckpoint: 目录引用的历史单项
    """
    async with state.database() as session:
        reference = await session.get(SkillDirectoryMember, (directory_id, name))
        assert reference is not None
        item = await session.get(SkillCheckpoint, reference.checkpoint_id)
        assert item is not None
        return item


async def test_initial_item_has_epoch_without_invented_backing_directory(
    stopped: RuntimeHarness,
) -> None:
    """
    独立库原始包初始化记录真实分支纪元，没有共同目录就不编造来源引用。

    :param stopped (RuntimeHarness): 已由真实预约建立分支的会话
    """
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.head_checkpoint_id is not None
        initial = await checkpoint(stopped, branch.head_checkpoint_id)
        assert initial.state_epoch == branch.epoch == 1
        assert initial.directory_epoch is None and initial.backing_directory_id is None


@pytest.mark.parametrize("directory_scope", [False, True])
async def test_reset_restore_preserve_history_and_report_creation_epochs(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, directory_scope: bool
) -> None:
    """
    重置恢复产生新纪元且不改写历史，查询把创建纪元与实时纪元分别展示。

    :param user_client (AsyncClient): 用户认证客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param directory_scope (bool): 是否同时推进完整目录纪元
    """
    saved = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"old"})
    )
    assert saved.result_checkpoint_id is not None
    old_directory = await checkpoint(stopped, saved.result_checkpoint_id)
    old_item = await member(stopped, old_directory.id)
    assert old_directory.directory_epoch == 1 and old_directory.state_epoch is None
    assert old_item.state_epoch == 1 and old_item.backing_directory_id == old_directory.id
    reset = await state_execute(
        stopped, tmp_path, await command(stopped, tmp_path, directory=directory_scope)
    )
    assert reset.result_checkpoint_id is not None
    reset_directory = await checkpoint(stopped, reset.result_checkpoint_id)
    reset_item = await member(stopped, reset_directory.id)
    assert reset_item.state_epoch == 2 and reset_item.backing_directory_id == reset_directory.id
    assert reset_directory.directory_epoch == 1 + int(directory_scope)
    restored = await state_execute(
        stopped,
        tmp_path,
        await command(
            stopped,
            tmp_path,
            checkpoint=old_directory.id if directory_scope else old_item.id,
            directory=directory_scope,
        ),
    )
    assert restored.result_checkpoint_id is not None
    restored_directory = await checkpoint(stopped, restored.result_checkpoint_id)
    restored_item = await member(stopped, restored_directory.id)
    assert (
        restored_item.state_epoch == 3
        and restored_item.backing_directory_id == restored_directory.id
    )
    assert restored_directory.directory_epoch == 1 + 2 * int(directory_scope)
    assert (await checkpoint(stopped, old_item.id)).state_epoch == 1
    assert (await checkpoint(stopped, old_directory.id)).directory_epoch == 1
    path = "/api/v1/skills/state/checkpoints/"
    info = (await user_client.get(path + str(old_item.id))).json()["data"]
    assert info["state_epoch"] == 1 and info["current_state_epoch"] == 3
    assert info["backing_directory_id"] == str(old_directory.id) and info["directory_epoch"] is None
    directory_info = (await user_client.get(path + str(old_directory.id))).json()["data"]
    assert directory_info["directory_epoch"] == 1
    assert directory_info["current_directory_epoch"] == 1 + 2 * int(directory_scope)
    members = (await user_client.get(path + str(old_directory.id) + "/members")).json()["data"][
        "items"
    ]
    assert members[0]["state_epoch"] == 1


async def test_late_input_uses_snapshot_epoch_and_exact_raw_backing(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    reset 后才到达的旧输入仍保留旧快照纪元，不借今天的分支纪元获得发布资格。

    :param stopped (RuntimeHarness): 原会话
    :param tmp_path (Path): 私有卷
    """
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path, directory=True))
    finalization_id = await ingest(stopped, tmp_path, {"learning/late": b"old epoch"})
    async with stopped.database() as session:
        receipt = await session.get(SkillFinalization, finalization_id)
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        assert receipt is not None and receipt.checkpoint_id is not None and snapshot is not None
        directory = await checkpoint(stopped, receipt.checkpoint_id)
        item = await member(stopped, directory.id)
        assert directory.directory_epoch == snapshot.directory_epoch == 1
        assert item.state_epoch == 1 and item.backing_directory_id == directory.id
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.epoch == 2
    result = await publish(stopped, tmp_path, finalization_id)
    assert result.status == "detached"
    assert (await checkpoint(stopped, item.id)).state_epoch == 1


async def test_deleted_item_retains_backing_even_without_directory_membership(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真实删除仍有同源单项和确切完整目录，成员缺失不能抹掉其纪元证据。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    result = await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning": None}))
    assert result.result_checkpoint_id is not None
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.head_checkpoint_id is not None
        item = await checkpoint(stopped, branch.head_checkpoint_id)
        assert item.backing_directory_id == result.result_checkpoint_id and item.state_epoch == 1
        assert (
            await session.get(SkillDirectoryMember, (result.result_checkpoint_id, "learning"))
            is None
        )


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_migration_results_record_exact_target_epoch_and_directory(
    stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    首次和增量迁移结果绑定新目录和精确目标分支，不沿用来源的 backing 身份。

    :param stopped (RuntimeHarness): 原会话
    :param tmp_path (Path): 私有卷
    :param mode (str): 迁移模式
    """
    previous = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"learned"})
    )
    source_revision, target_revision = await versions(stopped, tmp_path)
    result = (
        await prepare(stopped, tmp_path, await preparation_request(stopped, tmp_path))
        if mode == "forward"
        else await migrate(
            stopped, tmp_path, await request(stopped, tmp_path, source_revision, target_revision)
        )
    )
    assert (
        result.status == "ready"
        and result.result_checkpoint_id is not None
        and result.result_directory_id is not None
    )
    item = await checkpoint(stopped, result.result_checkpoint_id)
    directory = await checkpoint(stopped, result.result_directory_id)
    assert item.backing_directory_id == directory.id != previous.result_checkpoint_id
    assert item.state_epoch == directory.directory_epoch == 1
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, item.state_id)
        assert branch is not None and branch.base_revision_id == target_revision


async def test_manual_resolution_and_local_initialization_keep_real_origins(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    人工完整发布引用自己的结果目录，本地初始化只引用明确登记的原始目录。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    conflict = await pending(stopped, tmp_path)
    resolved = await choose(stopped, tmp_path, conflict, SkillResolutionChoice(use="incoming"))
    assert resolved.result_checkpoint_id is not None
    item = await member(stopped, resolved.result_checkpoint_id)
    assert item.state_epoch == 1 and item.backing_directory_id == resolved.result_checkpoint_id
    raw = await source(stopped, tmp_path)
    local = await register(stopped, tmp_path, raw)
    await activate(stopped, local)
    current = await new_session(stopped, tmp_path)
    async with stopped.database() as session:
        branch = await session.scalar(
            select(AccountSkillState).where(AccountSkillState.local_skill_id == local.id)
        )
        assert branch is not None and branch.head_checkpoint_id is not None
        initial = await checkpoint(stopped, branch.head_checkpoint_id)
        assert initial.state_epoch == 1 and initial.backing_directory_id == raw.id
    publication = await publish(
        current, tmp_path, await ingest(current, tmp_path, {"notes/memory": b"new"})
    )
    assert publication.result_checkpoint_id is not None
    changed = await member(stopped, publication.result_checkpoint_id, "notes")
    unchanged = await member(stopped, publication.result_checkpoint_id)
    assert (
        changed.backing_directory_id == publication.result_checkpoint_id
        and changed.state_epoch == 1
    )
    assert (
        unchanged.id == item.id and unchanged.backing_directory_id == resolved.result_checkpoint_id
    )


@pytest.mark.parametrize(
    "failure",
    [
        "state-zero",
        "directory-zero",
        "wrong-epoch-scope",
        "wrong-backing-scope",
        "wrong-content",
        "backing-item",
        "other-account",
        "other-user",
    ],
)
async def test_provenance_constraints_bind_scope_content_and_owner(
    stopped: RuntimeHarness, tmp_path: Path, failure: str
) -> None:
    """
    只有同用户同账户同完整内容的目录能成为 backing，纪元也必须属于正确范围。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param failure (str): 精确约束破坏方式
    """
    publication = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"value"})
    )
    assert publication.result_checkpoint_id is not None
    directory = await checkpoint(stopped, publication.result_checkpoint_id)
    item = await member(stopped, directory.id)
    if failure == "other-user":
        foreign = await runtime(stopped.database, tmp_path / "foreign")
        backing = foreign.directory
    elif failure == "other-account":
        account = await LibraryHarness(stopped.database, tmp_path, stopped.owner).account()
        async with stopped.database.begin() as session:
            session.add(
                AccountSkillDirectoryState(
                    user_id=stopped.owner, account_id=account, tool_type="claude"
                )
            )
            await session.flush()
            other = SkillCheckpoint(
                id=uuid4(),
                user_id=stopped.owner,
                account_id=account,
                scope="directory",
                content_digest=directory.content_digest,
                tree_digest=directory.tree_digest,
                directory_epoch=1,
            )
            session.add(other)
            backing = other.id
    else:
        backing = directory.id
    async with stopped.database.begin() as session:
        invalid = SkillCheckpoint(
            id=uuid4(),
            user_id=stopped.owner,
            account_id=stopped.account,
            scope="item",
            state_id=item.state_id,
            subtree_prefix="learning",
            content_digest=directory.content_digest,
            tree_digest=directory.tree_digest,
            state_epoch=1,
            backing_directory_id=backing,
        )
        if failure == "state-zero":
            invalid.state_epoch = 0
        elif failure == "directory-zero":
            invalid.scope, invalid.state_id, invalid.subtree_prefix = "directory", None, ""
            invalid.state_epoch, invalid.backing_directory_id, invalid.directory_epoch = (
                None,
                None,
                0,
            )
        elif failure == "wrong-epoch-scope":
            invalid.directory_epoch = 1
        elif failure == "wrong-backing-scope":
            invalid.backing_scope = "item"
        elif failure == "wrong-content":
            invalid.content_digest = stopped.tree
            invalid.tree_digest = stopped.tree
        elif failure == "backing-item":
            invalid.backing_directory_id = item.id
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(invalid)
                await session.flush()
    assert (await checkpoint(stopped, item.id)).backing_directory_id == directory.id


async def test_legacy_unknown_and_retired_content_preserve_explicit_metadata(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧数据不冒充当前纪元，内容退役不破坏确切 backing 的审计身份约束。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    legacy = (
        await user_client.get(f"/api/v1/skills/state/checkpoints/{stopped.directory}")
    ).json()["data"]
    assert legacy["directory_epoch"] is None and legacy["current_directory_epoch"] == 1
    result = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"retained"})
    )
    assert result.result_checkpoint_id is not None
    item = await member(stopped, result.result_checkpoint_id)
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    async with stopped.database.begin() as session:
        old = await session.get(SkillCheckpoint, result.result_checkpoint_id)
        assert old is not None
        old.retained, old.tree_digest = False, None
    historical = await checkpoint(stopped, item.id)
    assert (
        historical.backing_directory_id == result.result_checkpoint_id
        and historical.state_epoch == 1
    )
    response = await user_client.get(
        f"/api/v1/skills/state/checkpoints/{result.result_checkpoint_id}"
    )
    assert response.status_code == 200 and response.json()["data"]["directory_epoch"] == 1
    assert response.json()["data"]["storage_location"] == "expired"
