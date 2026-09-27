"""
验证内部迁移计划的范围替换、版本竞争、不可变重放及完整事务回滚。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

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
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_migration_resolution import SkillMigrationResolutionDraftView
from agent_remote_server.schemas.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_drafts import (
    MigrationResolutionDraftService,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


def edit_request(
    choice: SkillResolutionChoice | None = None, revision: int = 0, dry_run: bool = False
) -> SkillResolutionRequest:
    """
    构造独立键的严格编辑，不把候选完整误认为发布完成。

    :param choice (SkillResolutionChoice | None): 明确范围，默认整体输入侧
    :param revision (int): 已知旧版本
    :param dry_run (bool): 是否只预览
    :return SkillResolutionRequest: 完整请求
    """
    return SkillResolutionRequest(
        idempotency_key=str(uuid4()),
        expected_revision=revision,
        choice=choice or SkillResolutionChoice(use="incoming"),
        dry_run=dry_run,
    )


async def edit(
    state: RuntimeHarness,
    root: Path,
    identity: UUID,
    payload: SkillResolutionRequest,
    policy: SkillStoragePolicy | None = None,
) -> SkillMigrationResolutionDraftView:
    """
    每次新建服务和事务，验证结果完全由数据库恢复。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 私有卷
    :param identity (UUID): 原迁移
    :param payload (SkillResolutionRequest): 编辑请求
    :param policy (SkillStoragePolicy | None): 可选限额
    :return SkillMigrationResolutionDraftView: 已提交计划或只读预览
    """
    async with state.database.begin() as session:
        return await MigrationResolutionDraftService(
            session, PrivateObjectStore(root / "objects"), policy or SkillStoragePolicy()
        ).execute(state.owner, identity, payload)


async def saved_plan(state: RuntimeHarness, identity: UUID) -> tuple[int, list[object], int]:
    """
    检查真实计划及所属回执，不依赖服务返回值推断持久状态。

    :param state (RuntimeHarness): 原账户
    :param identity (UUID): 原迁移
    :return tuple[int, list[object], int]: 版本、选择摘要和回执数量
    """
    async with state.database() as session:
        migration = await session.get(SkillBranchPreparation, identity)
        assert migration is not None
        repository = SkillMigrationResolutionRepository(session)
        plan = await repository.plan(migration)
        choices: list[object] = [
            (row.path, row.unit_json, row.kind, row.tree_digest)
            for row in await repository.choices(migration)
        ]
        total = await session.scalar(
            select(func.count())
            .select_from(SkillMigrationResolutionOperation)
            .where(SkillMigrationResolutionOperation.migration_id == identity)
        )
        assert total is not None
        return plan.revision if plan else 0, choices, total


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_preview_is_read_only_and_complete_draft_never_publishes(
    stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    完整选择仍只保存计划，原始版本、所有 head、成功基线及内容预约保持原值。

    :param stopped (RuntimeHarness): 原会话
    :param tmp_path (Path): 私有卷
    :param mode (str): 首次或增量迁移
    """
    original = await pending(stopped, tmp_path, mode)
    identity = original.operation_id
    assert identity is not None
    before = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        usage_before = (usage.lock_version, usage.state_bytes, usage.state_reserved)
    payload = edit_request()
    preview = await edit(stopped, tmp_path, identity, payload.model_copy(update={"dry_run": True}))
    assert (
        preview.status == "preview" and preview.plan_revision == 0 and preview.operation_id is None
    )
    assert preview.candidate_complete and preview.target_modified and not preview.remaining
    assert preview.original_changes and preview.target_changes == preview.original_changes
    assert await saved_plan(stopped, identity) == (0, [], 0)
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        assert (usage.lock_version, usage.state_bytes, usage.state_reserved) == usage_before
    result = await edit(stopped, tmp_path, identity, payload)
    assert result.status == "planned" and result.plan_revision == 1 and result.operation_id
    assert result.result_tree_digest == preview.result_tree_digest
    assert result.target_revision_id == before.targets[0].revision_id
    assert (await saved_plan(stopped, identity))[0::2] == (1, 1)
    assert (await command(stopped, tmp_path)).expected == before and await counts(stopped) == rows
    async with stopped.database() as session:
        migration = await session.get(SkillBranchPreparation, identity)
        assert migration is not None and migration.status == "conflicted"
        assert migration.migration_sequence is None and migration.result_checkpoint_id is None
        assert migration.response_json == original.model_dump(mode="json")
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        assert (usage.state_bytes, usage.state_reserved) == usage_before[1:]


async def two_conflicts(state: RuntimeHarness, root: Path) -> UUID:
    """
    发布两个分支的真实独立改动，建立需要逐项保存的两处冲突。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 私有卷
    :return UUID: 真实增量迁移冲突身份
    """
    await publish(state, root, await ingest(state, root, {"learning/a": b"a", "learning/b": b"b"}))
    late = await new_session(state, root)
    source, target = await versions(state, root)
    await migrate(state, root, await request(state, root, source, target))
    newer = await new_session(state, root)
    await publish(
        newer,
        root,
        await ingest(newer, root, {"learning/a": b"ours", "learning/b": b"ours"}),
    )
    await publish(
        late,
        root,
        await ingest(late, root, {"learning/a": b"theirs", "learning/b": b"theirs"}),
    )
    migration = await migrate(state, root, await request(state, root, source, target))
    identity = migration.operation_id
    assert identity is not None and {conflict.path for conflict in migration.conflicts} == {
        "learning/a",
        "learning/b",
    }
    return identity


async def test_partial_edits_keep_independent_choices_and_whole_replaces_all(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两条真实内容冲突逐项保存，单项重选不丢另一项，整体选择清除全部旧范围。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    a = SkillResolutionChoice(path="learning/a", use="incoming")
    b = SkillResolutionChoice(path="learning/b", use="current")
    first = await edit(stopped, tmp_path, identity, edit_request(a))
    assert not first.candidate_complete and first.result_tree_digest is None
    assert (
        first.target_modified is None
        and first.original_changes is None
        and first.directory_changes is None
    )
    assert [conflict.path for conflict in first.remaining] == ["learning/b"]
    preview = await edit(stopped, tmp_path, identity, edit_request(b, 1, True))
    assert preview.candidate_complete and preview.plan_revision == 1
    assert (await saved_plan(stopped, identity))[0::2] == (1, 1)
    second = await edit(stopped, tmp_path, identity, edit_request(b, 1))
    assert second.candidate_complete and set(second.choices) == {a, b}
    replacement = SkillResolutionChoice(path="learning/a", use="current")
    third = await edit(stopped, tmp_path, identity, edit_request(replacement, 2))
    assert set(third.choices) == {replacement, b} and third.target_changes == []
    whole = await edit(stopped, tmp_path, identity, edit_request(revision=3))
    assert len(whole.choices) == 1 and whole.choices[0].whole and whole.plan_revision == 4
    narrow = await edit(stopped, tmp_path, identity, edit_request(a, 4))
    assert narrow.choices == [a] and not narrow.candidate_complete


@pytest.mark.parametrize("linked", [False, True])
async def test_concurrent_duplicate_and_competing_revisions(
    stopped: RuntimeHarness, tmp_path: Path, linked: bool
) -> None:
    """
    同键并发只接受一次，不同键争用同一版本只有一方能保存。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param linked (bool): 是否完整关联单元
    """
    original = await pending(stopped, tmp_path, linked=linked)
    identity = original.operation_id
    assert identity is not None
    payload = edit_request()
    first, duplicate = await asyncio.gather(
        edit(stopped, tmp_path, identity, payload), edit(stopped, tmp_path, identity, payload)
    )
    assert first == duplicate and first.plan_revision == 1
    results = await asyncio.gather(
        edit(stopped, tmp_path, identity, edit_request(SkillResolutionChoice(use="current"), 1)),
        edit(stopped, tmp_path, identity, edit_request(revision=1)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SkillMigrationResolutionDraftView) for result in results) == 1
    failures = [result for result in results if isinstance(result, SkillContentError)]
    assert len(failures) == 1 and failures[0].code == "PLAN_REVISION_CONFLICT"
    assert (await saved_plan(stopped, identity))[0::2] == (2, 2)


async def test_original_key_replays_after_edit_and_reset_with_separate_live_status(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    历史回执不会应用旧选择，失效后可查原响应而新请求不能恢复已清除状态。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    payload = edit_request()
    first = await edit(stopped, tmp_path, identity, payload)
    await edit(stopped, tmp_path, identity, edit_request(SkillResolutionChoice(use="current"), 1))
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    before = (await command(stopped, tmp_path)).expected
    assert await edit(stopped, tmp_path, identity, payload) == first
    async with stopped.database.begin() as session:
        receipt = await MigrationResolutionDraftService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).receipt(stopped.owner, payload.idempotency_key)
        assert receipt.result == first and receipt.current_status == "superseded"
    with pytest.raises(SkillContentError, match="no longer active"):
        await edit(stopped, tmp_path, identity, edit_request(revision=2))
    assert (await command(stopped, tmp_path)).expected == before
    assert (await saved_plan(stopped, identity))[0::2] == (2, 2)


@pytest.mark.parametrize("change", ["choice", "migration", "version"])
async def test_same_key_different_request_is_rejected(
    stopped: RuntimeHarness, tmp_path: Path, change: str
) -> None:
    """
    键绑定迁移和完整原请求，不能作为其他选择或版本的可重用授权。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param change (str): 改写的请求部分
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    identity = original.operation_id
    payload = edit_request()
    await edit(stopped, tmp_path, identity, payload)
    if change == "choice":
        payload = payload.model_copy(update={"choice": SkillResolutionChoice(use="current")})
    elif change == "version":
        payload = payload.model_copy(update={"expected_revision": 1})
    else:
        other = await migrate(
            stopped,
            tmp_path,
            await request(
                stopped,
                tmp_path,
                original.before.source.revision_id,
                original.before.target.revision_id,
            ),
        )
        assert other.operation_id is not None
        identity = other.operation_id
    with pytest.raises(SkillContentError) as error:
        await edit(stopped, tmp_path, identity, payload)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("failure", ["scope", "quota", "stale", "revision"])
async def test_validation_failure_preserves_plan_and_receipts(
    stopped: RuntimeHarness, tmp_path: Path, failure: str
) -> None:
    """
    无效范围、超额、过期比较和旧版本都不能覆盖已有计划或新增回执。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param failure (str): 失败条件
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    identity = original.operation_id
    await edit(stopped, tmp_path, identity, edit_request())
    before = await saved_plan(stopped, identity)
    payload = edit_request(revision=1)
    if failure == "scope":
        payload = edit_request(SkillResolutionChoice(path="unrelated", use="incoming"), 1)
    elif failure == "stale":
        await pin(stopped, tmp_path, original.before.source.revision_id)
    elif failure == "revision":
        payload = edit_request()
    with pytest.raises(SkillContentError) as error:
        await edit(
            stopped,
            tmp_path,
            identity,
            payload,
            SkillStoragePolicy(checkpoint_bytes=1) if failure == "quota" else None,
        )
    assert (
        error.value.code
        == {
            "scope": "INVALID_RESOLUTION",
            "quota": "QUOTA_EXCEEDED",
            "stale": "STATE_PRECONDITION_CHANGED",
            "revision": "PLAN_REVISION_CONFLICT",
        }[failure]
    )
    assert await saved_plan(stopped, identity) == before


@pytest.mark.parametrize("existing", [False, True])
async def test_late_receipt_failure_rolls_back_saved_plan_even_if_outer_commits(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    """
    计划交换后回执写入失败，外层捕获并提交也不能留下新版本或新选择。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 注入最后一步失败
    :param existing (bool): 是否已保存旧计划
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    if existing:
        await edit(stopped, tmp_path, identity, edit_request())
    before = await saved_plan(stopped, identity)
    original_save = SkillMigrationResolutionRepository.save_operation

    async def fail_after_save(
        self: SkillMigrationResolutionRepository, operation: SkillMigrationResolutionOperation
    ) -> None:
        """
        先刷新真实回执再失败，覆盖嵌套保存点已经释放后的完整回滚。

        :param operation (SkillMigrationResolutionOperation): 待保存回执
        """
        await original_save(self, operation)
        raise RuntimeError("late receipt failure")

    monkeypatch.setattr(SkillMigrationResolutionRepository, "save_operation", fail_after_save)
    async with stopped.database.begin() as session:
        service = MigrationResolutionDraftService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        with pytest.raises(RuntimeError, match="late receipt failure"):
            await service.execute(
                stopped.owner,
                identity,
                edit_request(SkillResolutionChoice(use="current"), before[0]),
            )
    assert await saved_plan(stopped, identity) == before


async def test_removed_custom_choice_need_not_reverify_but_retained_choice_must(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已损坏人工内容可被明确整体选择替换，但预览保留它时仍拒绝无效字节。

    :param user_client (AsyncClient): 真实内容接口
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    upload = await begin(user_client, identity)
    entry = file_entry(b"resolved")
    path = f"{BASE}/{identity}/uploads/{upload}"
    assert (
        await user_client.put(path + "/files/" + entry.sha256, content=b"resolved")
    ).status_code == 200
    completed = await user_client.post(path + "/complete")
    custom = SkillResolutionChoice(
        path="learning/a", file_tree_digest=completed.json()["data"]["tree_digest"]
    )
    await edit(stopped, tmp_path, identity, edit_request(custom))
    blob = tmp_path / "objects" / str(stopped.owner) / entry.sha256[:2] / entry.sha256
    blob.unlink()
    with pytest.raises(SkillContentError) as error:
        await edit(
            stopped,
            tmp_path,
            identity,
            edit_request(SkillResolutionChoice(path="learning/b", use="incoming"), 1, True),
        )
    assert error.value.code == "CONTENT_INCOMPLETE"
    replaced = await edit(stopped, tmp_path, identity, edit_request(revision=1))
    assert replaced.candidate_complete and replaced.plan_revision == 2
    assert replaced.choices == [SkillResolutionChoice(use="incoming")]


async def test_owner_and_typed_receipt_boundaries(stopped: RuntimeHarness, tmp_path: Path) -> None:
    """
    其他用户不能读回执或编辑，错误操作类型不能被默认字段解释成有效计划。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    payload = edit_request()
    result = await edit(stopped, tmp_path, identity, payload)
    other = await user(stopped.database)
    async with stopped.database.begin() as session:
        service = MigrationResolutionDraftService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        with pytest.raises(SkillContentError) as error:
            await service.receipt(other, payload.idempotency_key)
        assert error.value.code == "OPERATION_NOT_FOUND"
        with pytest.raises(SkillContentError) as error:
            await service.execute(other, identity, payload)
        assert error.value.code == "CONFLICT_NOT_FOUND"
    async with stopped.database.begin() as session:
        assert await session.get(SkillStorageUsage, other) is None
        operation = await session.get(SkillMigrationResolutionOperation, result.operation_id)
        assert operation is not None
        operation.response_json = {**operation.response_json, "operation_kind": "another_kind"}
    async with stopped.database.begin() as session:
        service = MigrationResolutionDraftService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        )
        with pytest.raises(SkillContentError) as error:
            await service.receipt(stopped.owner, payload.idempotency_key)
        assert error.value.code == "OPERATION_KIND_MISMATCH"
