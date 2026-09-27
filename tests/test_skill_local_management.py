"""
验证账户本地查询、规则消歧及运行状态不受配置变更影响。
"""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import delete, select
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_library import LibraryHarness
from test_skill_local import activate, register, source
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve

from agent_remote_server.models.skill_library import SkillLibrary
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshotItem
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.schemas.skill_results import SkillLocalView
from agent_remote_server.services.skills.content import SkillContentError


async def test_local_query_scope_and_disabled_visibility(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    停用项只在精确账户可见，且无用户库记录时仍可查询。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(prepared.database, tmp_path, await user(prepared.database))
    prepared = replace(prepared, owner=library.owner, account=await library.account())
    async with prepared.database.begin() as session:
        session.add(
            AccountSkillDirectoryState(
                user_id=prepared.owner, account_id=prepared.account, tool_type="claude"
            )
        )
    item = await register(prepared, tmp_path, await source(prepared, tmp_path))
    await activate(prepared, item, enabled=False)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    other = await library.account()
    async with prepared.database.begin() as session:
        await session.execute(delete(SkillLibrary).where(SkillLibrary.user_id == prepared.owner))
        service = library.service(session)
        assert not (await service.list_skills(prepared.owner)).local_items
        view = await service.list_skills(
            prepared.owner, scope=SkillScope(account_id=prepared.account)
        )
        assert view.generation == 0 and len(view.local_items) == 1
        assert not view.local_items[0].enabled
        assert not await SkillLocalRepository(session).active(prepared.owner, prepared.account)
        details = await service.info(
            prepared.owner, "notes", scope=SkillScope(account_id=prepared.account)
        )
        assert isinstance(details, SkillLocalView)
        assert details.id == item.id and details.revisions[0].subtree_prefix == "notes"
        assert details.effective.enabled_source == "account" and not details.effective.included
        for scope, code in [
            (None, "LOCAL_SKILL_SCOPE_REQUIRED"),
            (SkillScope(tools=("claude",)), "LOCAL_SKILL_SCOPE_REQUIRED"),
            (SkillScope(account_id=other), "SKILL_NOT_FOUND"),
        ]:
            with pytest.raises(SkillContentError) as error:
                await service.info(prepared.owner, str(item.id), scope=scope)
            assert error.value.code == code
        assert not (
            await service.list_skills(prepared.owner, scope=SkillScope(account_id=other))
        ).local_items
        with pytest.raises(SkillContentError) as error:
            await service.info(uuid4(), str(item.id), scope=SkillScope(account_id=prepared.account))
        assert error.value.code == "ACCOUNT_NOT_FOUND"


async def test_local_rules_preserve_snapshot_heads_and_original_receipt(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    启停及继承只推进配置代数，重放返回原回执并保留原快照。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    item = await register(prepared, tmp_path, await source(prepared, tmp_path))
    await activate(prepared, item)
    assert item.default_revision_id is not None
    snapshot = await reserve(prepared, tmp_path)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    async with prepared.database() as session:
        branch = await SkillLocalRepository(session).branch(
            prepared.owner, prepared.account, item.id, item.default_revision_id
        )
        assert branch is not None
        branch_id = branch.id
        before = (branch.epoch, branch.head_checkpoint_id)
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory_before = (directory.epoch, directory.head_checkpoint_id)
    request = SkillRuleRequest(
        command="disable",
        skill=str(item.id),
        scope=SkillScope(account_id=prepared.account),
        idempotency_key=str(uuid4()),
        expected_generation=await library.generation(),
    )
    disabled = await library.execute(request)
    assert disabled.data.changed and disabled.data.skill_ids == [item.id]
    assert [target.account_id for target in disabled.data.targets] == [prepared.account]
    inherited = await library.execute(
        SkillRuleRequest(
            command="inherit",
            field="all",
            skill="notes",
            scope=request.scope,
            idempotency_key=str(uuid4()),
            expected_generation=disabled.data.generation,
        )
    )
    assert inherited.data.changed
    assert (await library.execute(request)) == disabled
    again = await library.execute(
        SkillRuleRequest(
            command="enable",
            skill="notes",
            scope=request.scope,
            idempotency_key=str(uuid4()),
            expected_generation=inherited.data.generation,
        )
    )
    assert not again.data.changed and again.data.generation == inherited.data.generation
    assert (await reserve(prepared, tmp_path)).id == snapshot.id
    async with prepared.database() as session:
        branch = await session.get(AccountSkillState, branch_id)
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert branch is not None and (branch.epoch, branch.head_checkpoint_id) == before
        assert (
            directory is not None
            and (directory.epoch, directory.head_checkpoint_id) == directory_before
        )
        assert await session.scalar(
            select(SessionSkillSnapshotItem.snapshot_id).where(
                SessionSkillSnapshotItem.snapshot_id == snapshot.id,
                SessionSkillSnapshotItem.state_id == branch_id,
            )
        )


@pytest.mark.parametrize(
    "command,field,revision",
    [("pin", "all", "r1"), ("unpin", "all", None), ("inherit", "revision", None)],
)
async def test_local_rejects_version_rules_without_generation_change(
    prepared: RuntimeHarness, tmp_path: Path, command: str, field: str, revision: str | None
) -> None:
    """
    版本规则不能套用本地来源，拒绝后配置不变。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    :param command (str): 不受支持的命令
    :param field (str): 继承字段
    :param revision (str | None): 固定版本选择
    """
    item = await register(prepared, tmp_path, await source(prepared, tmp_path))
    await activate(prepared, item)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    generation = await library.generation()
    request = SkillRuleRequest.model_validate(
        dict(
            command=command,
            field=field,
            revision=revision,
            skill=str(item.id),
            scope=dict(account_id=str(prepared.account)),
            idempotency_key=str(uuid4()),
            expected_generation=generation,
        )
    )
    with pytest.raises(SkillContentError) as error:
        await library.execute(request)
    assert error.value.code == "LOCAL_SKILL_COMMAND_UNSUPPORTED"
    assert await library.generation() == generation
    async with prepared.database() as session:
        stored = await session.get(AccountLocalSkill, item.id)
        assert stored is not None and stored.enabled


async def test_library_local_name_collision_requires_stable_identity(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未限定名称及账户同名均拒绝，明确库身份仍可使用。

    :param prepared (RuntimeHarness): 测试账户
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    installed = await library.info()
    item = await register(
        prepared, tmp_path, await source(prepared, tmp_path, "learning"), "learning"
    )
    await activate(prepared, item)
    for scope in [
        SkillScope(),
        SkillScope(tools=("claude",)),
        SkillScope(account_id=prepared.account),
    ]:
        with pytest.raises(SkillContentError) as error:
            await library.execute(
                SkillRuleRequest(
                    command="disable",
                    skill="learning",
                    scope=scope,
                    expected_generation=await library.generation(),
                    idempotency_key=str(uuid4()),
                )
            )
        assert error.value.code == "SKILL_SOURCE_CONFLICT"
        async with prepared.database() as session:
            with pytest.raises(SkillContentError) as error:
                await library.service(session).info(prepared.owner, "learning", scope=scope)
            assert error.value.code == "SKILL_SOURCE_CONFLICT"
    result = await library.execute(
        SkillRuleRequest(
            command="disable",
            skill=str(installed.id),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    assert result.data.skill_ids == [installed.id]
