"""
以真实内容和业务服务生成有版本、覆盖、学习数据及未解决冲突的恢复语料。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import user
from test_skill_library import LibraryHarness
from test_skill_resolution_service import choose, pending
from test_skill_session_admission import ready
from test_skill_snapshots import prepare_runtime, reserve

from agent_remote_server.models import Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice


@dataclass(frozen=True)
class RestoreSeed:
    """
    保留恢复后查询所需的公开身份，不把 ORM 会话或内容缓存当作恢复来源。
    """

    state: RuntimeHarness
    publication_id: UUID
    choice_key: str
    other_owner: UUID


async def seed_restore(database: async_sessionmaker[AsyncSession], root: Path) -> RestoreSeed:
    """
    会话进程终止是显式夹具；版本、上传、发布、冲突及计划均由实际服务持久化。

    :param database (async_sessionmaker[AsyncSession]): 已升级到正式迁移头的源数据库
    :param root (Path): 源内容卷
    :return RestoreSeed: 具有可继续处理的未完成冲突计划的身份
    """
    state = await prepare_runtime(database, root)
    state.snapshot = (await reserve(state, root)).id
    async with database.begin() as session:
        row = await session.get(Session, state.session)
        snapshot = await session.get(SessionSkillSnapshot, state.snapshot)
        assert row is not None and snapshot is not None
        row.status, snapshot.status = "stopped", "started"
    await ready(state)
    library = LibraryHarness(database, root, state.owner)
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            stage=True,
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    rules: tuple[tuple[Literal["disable", "enable", "pin"], SkillScope, str | None], ...] = (
        ("disable", SkillScope(tools=("claude",)), None),
        ("enable", SkillScope(account_id=state.account), None),
        ("pin", SkillScope(account_id=state.account), "r1"),
        ("pin", SkillScope(tools=("claude",)), "r2"),
    )
    for command, scope, revision in rules:
        await library.execute(
            SkillRuleRequest(
                command=command,
                scope=scope,
                revision=revision,
                skill="learning",
                expected_generation=await library.generation(),
                idempotency_key=str(uuid4()),
            )
        )
    first = await ingest(
        state,
        root,
        {
            "learning/removed.txt": b"removed before backup",
            "notes/SKILL.md": b"---\nname: notes\ndescription: Saved binary state\n---\nNotes\n",
            "notes/state.bin": b"\x00\xffsaved-binary-state\x00",
            "root-helper.txt": b"account-directory auxiliary state",
        },
    )
    assert (await publish(state, root, first)).status == "published"
    deleted = await new_session(state, root)
    removal = await ingest(deleted, root, {"learning/removed.txt": None})
    assert (await publish(deleted, root, removal)).status == "published"
    current = await new_session(state, root)
    unclean = await new_session(state, root)
    recovery = await ingest(
        unclean, root, {"learning/recovered.txt": b"unclean retained"}, unclean=True
    )
    assert (await publish(unclean, root, recovery)).status == "detached"
    publication = await pending(current, root)
    key = str(uuid4())
    choice = await choose(
        current,
        root,
        publication,
        SkillResolutionChoice(path="learning/one", use="current"),
        key=key,
    )
    assert choice.status == "pending" and choice.plan_revision == 1
    other_owner = await user(database)
    other = LibraryHarness(database, root, other_owner)
    await other.add(await other.candidate())
    return RestoreSeed(current, publication.id, key, other_owner)
