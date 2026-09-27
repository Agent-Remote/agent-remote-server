"""
在同一组持久事务中验证候选发布、单账户 pin 与两个实际快照的版本隔离。
"""

import io
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from skill_publication_support import baseline, new_session
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_preparation import prepare
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve
from test_skill_state_commands import command

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshotItem
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
)
from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_preparation import SkillPreparationRequest


async def another_managed_account(state: RuntimeHarness, root: Path) -> RuntimeHarness:
    """
    为同用户建立独立空目录；工作区模板沿用原会话，账户状态从不复制。

    :param state (RuntimeHarness): 原用户和会话模板
    :param root (Path): 私有内容卷
    :return RuntimeHarness: 使用独立账户和目录的准备身份
    """
    account = await LibraryHarness(state.database, root, state.owner).account()
    directory = uuid4()
    async with state.database.begin() as session:
        session.add(
            AccountSkillDirectoryState(user_id=state.owner, account_id=account, tool_type="claude")
        )
        await session.flush()
        session.add(
            SkillCheckpoint(
                id=directory,
                user_id=state.owner,
                account_id=account,
                scope="directory",
                content_digest=state.tree,
                tree_digest=state.tree,
            )
        )
        await session.flush()
        row = await session.get(AccountSkillDirectoryState, account)
        assert row is not None
        row.mode, row.head_checkpoint_id = "managed_v1", directory
    return replace(state, account=account, directory=directory)


async def prepare_tracked(state: RuntimeHarness, root: Path) -> None:
    """
    通过公开前置条件和真实准备事务进入当前选定分支。

    :param state (RuntimeHarness): 当前账户
    :param root (Path): 私有内容卷
    """
    current = await command(state, root, skill="tracked")
    result = await prepare(
        state,
        root,
        SkillPreparationRequest(
            idempotency_key=str(uuid4()), selector=current.selector, expected=current.expected
        ),
    )
    assert result.status == "ready"


async def assert_selected_snapshot(
    state: RuntimeHarness, root: Path, revision: UUID, content: bytes
) -> None:
    """
    从已预约快照检查持久分支、解析结果与真实物化字节，重试仍复用原身份。

    :param state (RuntimeHarness): 已预约会话
    :param root (Path): 私有内容卷
    :param revision (UUID): 应固定的原始版本
    :param content (bytes): 应物化的技能正文
    """
    snapshot = await reserve(state, root)
    assert snapshot.id == state.snapshot
    async with state.database() as session:
        item = await session.scalar(
            select(SessionSkillSnapshotItem).where(
                SessionSkillSnapshotItem.snapshot_id == state.snapshot,
                SessionSkillSnapshotItem.entry_name == "tracked",
            )
        )
        assert item is not None and item.account_id == state.account
        branch = await session.get(AccountSkillState, item.state_id)
        assert branch is not None and branch.base_revision_id == revision
        assert item.resolution_json["revision_id"] == str(revision)
        manifest = await baseline(state, root)
        entry = next(entry for entry in manifest.entries if entry.path == "tracked/SKILL.md")
        output = io.BytesIO()
        await content_service(session, root).read_file(
            state.owner, "state", snapshot.tree_digest, entry.sha256, output
        )
        assert output.getvalue() == content


async def test_stage_r4_pin_a_reserves_a_r4_b_r3_and_keeps_tracking(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两账户先实际使用 r3，暂存 r4 并只固定 A 后各自预约 r4/r3，默认版本与 Git 跟踪不变。

    :param stopped (RuntimeHarness): 已有独立账户与真实会话
    :param tmp_path (Path): 私有内容卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="tracked", origin="git", version="one"))
    for version in ("two", "three"):
        await library.execute(
            SkillUpdateRequest(
                skill="tracked",
                item=await library.candidate(name="tracked", origin="git", version=version),
                expected_generation=await library.generation(),
                idempotency_key=str(uuid4()),
            )
        )
    before = await library.info("tracked")
    assert before.default_revision_id is not None
    other = await another_managed_account(stopped, tmp_path)
    for account in (stopped, other):
        await prepare_tracked(account, tmp_path)
        await new_session(account, tmp_path)
    candidate = await library.candidate(name="tracked", origin="git", version="four")
    staged = await library.execute(
        SkillUpdateRequest(
            skill="tracked",
            item=candidate,
            stage=True,
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    r4 = staged.data.revision_ids[0]
    assert not staged.data.targets
    await library.execute(
        SkillRuleRequest(
            command="pin",
            skill="tracked",
            revision="r4",
            scope=SkillScope(account_id=stopped.account),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    generation = await library.generation()
    await prepare_tracked(stopped, tmp_path)
    await prepare_tracked(other, tmp_path)
    a, b = await new_session(stopped, tmp_path), await new_session(other, tmp_path)
    assert a.snapshot != b.snapshot
    await assert_selected_snapshot(
        a, tmp_path, r4, "---\nname: tracked\ndescription: 学习\n---\nfour\n".encode()
    )
    await assert_selected_snapshot(
        b,
        tmp_path,
        before.default_revision_id,
        "---\nname: tracked\ndescription: 学习\n---\nthree\n".encode(),
    )
    after = await library.info("tracked")
    assert after.default_revision_id == before.default_revision_id
    assert after.tracking == before.tracking
    assert await library.generation() == generation
    async with stopped.database() as session:
        rows = (
            await session.scalars(
                select(AccountSkillState).where(
                    AccountSkillState.account_id == other.account,
                    AccountSkillState.base_revision_id == r4,
                )
            )
        ).all()
        assert not rows
