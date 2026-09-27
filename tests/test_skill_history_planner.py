"""
验证完整历史退役计划、精确重验、范围隔离和大批次保存点原子性。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_checkpoint_retirement import historical_selection
from test_skill_content_service import database as database
from test_skill_directory_compaction import fingerprint
from test_skill_finalization import stopped as stopped
from test_skill_preparation import pin
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import history_records
from agent_remote_server.services.skills.retention.planner import SkillHistoryRetirementPlanner
from agent_remote_server.services.skills.retention.planning import (
    HistoryRetirementPlan,
    retained_history,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def preview_history(
    state: RuntimeHarness, *keys: RetentionKey, early: bool = True
) -> HistoryRetirementPlan:
    """
    在最终提交的只读事务中预览，捕获错误加入 ORM 的假设修改。

    :param state (RuntimeHarness): 原始账户
    :param keys (RetentionKey): 精确初始选择
    :param early (bool): 是否明确提前结束等待
    :return HistoryRetirementPlan: 完整消费者计划
    """
    async with state.database.begin() as session:
        plan = await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).preview(
            state.owner, state.account, keys, all_unreferenced=early
        )
        assert not session.dirty and not session.new and not session.deleted
        return plan


async def apply_history(
    state: RuntimeHarness, plan: HistoryRetirementPlan
) -> tuple[RetentionKey, ...]:
    """
    提交原始精确计划，不能重新选择当前历史或跳过等待。

    :param state (RuntimeHarness): 已授权账户
    :param plan (HistoryRetirementPlan): 原始完整预览
    :return tuple[RetentionKey, ...]: 实际退役内容身份
    """
    async with state.database.begin() as session:
        return await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).apply(
            state.owner, plan
        )


async def test_history_plan_expands_consumers_and_preserves_original_evidence(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    单个旧 head 展开目录及比较消费者，原始证据不改写，全部退役后保持诚实过期和重查语义。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    _, head, _ = await historical_selection(stopped, tmp_path)
    key = RetentionKey("checkpoint", str(head))
    before = await fingerprint(stopped)
    plan = await preview_history(stopped, key)
    assert plan == await preview_history(stopped, key)
    assert plan.ready and len(plan.entries) > 1 and plan.dependencies
    assert any(row.key.kind == "publication" for row in plan.entries)
    assert any(identity == head for _, identity, _ in plan.expiring_branches)
    assert await fingerprint(stopped) == before
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        records = history_records(index)
        original = {
            row.id: (row.content_digest, row.parent_id, row.backing_directory_id)
            for row in index.checkpoints
        }
        assert all(retained_history(records[row.key]) for row in plan.entries)
    assert await apply_history(stopped, plan) == tuple(row.key for row in plan.entries)
    async with stopped.database() as session:
        index = await SkillRetentionRepository(session).load(stopped.owner)
        records = history_records(index)
        assert all(not retained_history(records[row.key]) for row in plan.entries)
        assert original == {
            row.id: (row.content_digest, row.parent_id, row.backing_directory_id)
            for row in index.checkpoints
        }
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.expired and branch.head_checkpoint_id == head
    with pytest.raises(SkillContentError) as error:
        await apply_history(stopped, plan)
    assert error.value.code == "HEAD_CHANGED"
    noop = await preview_history(stopped, key)
    assert noop.ready and len(noop.entries) == 1 and not noop.entries[0].retained
    assert await apply_history(stopped, noop) == ()


async def test_history_plan_requires_each_consumers_real_deadline(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    初始选择到期不足以退役尚在等待的消费者，整组真实到期后才可一次提交。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    _, head, _ = await historical_selection(stopped, tmp_path)
    key = RetentionKey("checkpoint", str(head))
    plan = await preview_history(stopped, key, early=False)
    assert not plan.ready and any("waiting" in row.blockers for row in plan.entries)
    async with stopped.database.begin() as session:
        row = await session.get(SkillCheckpoint, head)
        assert row is not None
        row.retention_released_at = datetime.now(UTC) - timedelta(days=31)
    partial = await preview_history(stopped, key, early=False)
    assert not partial.ready and not next(row for row in partial.entries if row.key == key).blockers
    before = await fingerprint(stopped)
    with pytest.raises(SkillContentError) as error:
        await apply_history(stopped, partial)
    assert error.value.code == "HISTORY_REFERENCED"
    assert (await fingerprint(stopped))[1:] == before[1:]
    async with stopped.database.begin() as session:
        records = history_records(await SkillRetentionRepository(session).load(stopped.owner))
        for item in partial.entries:
            records[item.key].retention_released_at = datetime.now(UTC) - timedelta(days=31)
    ready = await preview_history(stopped, key, early=False)
    assert ready.ready
    assert await apply_history(stopped, ready) == tuple(row.key for row in ready.entries)


async def test_history_plan_rejects_new_pin_and_wrong_owner(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已确认计划不能在新 pin 后退休内容，错误账户或身份的混合选择不返回部分结果。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    _, head, revision = await historical_selection(stopped, tmp_path)
    key = RetentionKey("checkpoint", str(head))
    plan = await preview_history(stopped, key)
    async with stopped.database.begin() as session:
        service = SkillHistoryRetirementPlanner(session, SkillStoragePolicy())
        for owner, account, keys in (
            (uuid4(), stopped.account, (key,)),
            (stopped.owner, uuid4(), (key,)),
            (stopped.owner, stopped.account, (key, RetentionKey("checkpoint", str(uuid4())))),
            (stopped.owner, stopped.account, (RetentionKey("revision", str(revision)),)),
        ):
            with pytest.raises(SkillContentError) as error:
                await service.preview(owner, account, keys, all_unreferenced=True)
            assert error.value.code == "HISTORY_NOT_FOUND"
        with pytest.raises(SkillContentError) as error:
            await service.apply(uuid4(), plan)
        assert error.value.code == "HISTORY_NOT_FOUND"
    await pin(stopped, tmp_path, revision)
    with pytest.raises(SkillContentError) as error:
        await apply_history(stopped, plan)
    assert error.value.code == "HEAD_CHANGED"
    protected = await preview_history(stopped, key)
    assert not protected.ready and any("protected" in row.blockers for row in protected.entries)


async def add_backed_history(state: RuntimeHarness, head_id: UUID, count: int) -> tuple[UUID, ...]:
    """
    为真实旧 backing 添加多个独立保留视图，构造跨越旧批量边界的循环依赖集合。

    :param state (RuntimeHarness): 原始账户
    :param head_id (UUID): 已解除保护的旧 head
    :param count (int): 额外历史数量
    :return tuple[UUID, ...]: 新增真实视图身份
    """
    identities = tuple(uuid4() for _ in range(count))
    async with state.database.begin() as session:
        head = await session.get(SkillCheckpoint, head_id)
        assert head is not None and head.backing_directory_id is not None
        for identity in identities:
            session.add(
                SkillCheckpoint(
                    id=identity,
                    user_id=state.owner,
                    account_id=state.account,
                    scope="item",
                    state_id=head.state_id,
                    subtree_prefix=head.subtree_prefix,
                    content_digest=head.content_digest,
                    tree_digest=head.tree_digest,
                    backing_directory_id=head.backing_directory_id,
                )
            )
    return identities


async def test_history_plan_rejects_new_consumer_before_any_mutation(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新保留视图扩大依赖闭包后原计划失效，捕获异常后提交也不能留下半次退役。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    _, head, _ = await historical_selection(stopped, tmp_path)
    plan = await preview_history(stopped, RetentionKey("checkpoint", str(head)))
    added = await add_backed_history(stopped, head, 1)
    before = await fingerprint(stopped)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).apply(
                stopped.owner, plan
            )
        assert error.value.code == "HEAD_CHANGED"
    assert (await fingerprint(stopped))[1:] == before[1:]
    updated = await preview_history(stopped, *plan.requested)
    assert RetentionKey("checkpoint", str(added[0])) in {row.key for row in updated.entries}
    assert updated.ready


async def test_history_plan_retires_more_than_one_thousand_as_one_transaction(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    完整依赖超过原上限仍整组预检，外层回滚恢复全部身份，成功提交不会截断第一批。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    _, head, _ = await historical_selection(stopped, tmp_path)
    added = await add_backed_history(stopped, head, 1001)
    plan = await preview_history(stopped, RetentionKey("checkpoint", str(head)))
    assert plan.ready and len(plan.entries) > 1000
    assert set(added) <= {
        UUID(row.key.identity) for row in plan.entries if row.key.kind == "checkpoint"
    }
    before = await fingerprint(stopped)
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with stopped.database.begin() as session:
            await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).apply(
                stopped.owner, plan
            )
            raise RuntimeError("outer rollback")
    assert (await fingerprint(stopped))[1:] == before[1:]
    result = await apply_history(stopped, plan)
    assert result == tuple(row.key for row in plan.entries)


async def test_history_plan_rolls_back_completed_retirement_when_outer_caller_catches_failure(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    退役已经刷新后再失败，调用方捕获并提交外层也不能遗留内容退役或 expired 标记。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入内部退役完成后的失败
    """
    from agent_remote_server.services.skills.retention.retire import SkillHistoryRetirementService

    _, head, _ = await historical_selection(stopped, tmp_path)
    plan = await preview_history(stopped, RetentionKey("checkpoint", str(head)))
    before = await fingerprint(stopped)
    original = SkillHistoryRetirementService.retire

    async def retire_then_fail(
        self: SkillHistoryRetirementService,
        user_id: UUID,
        account_id: UUID,
        keys: tuple[RetentionKey, ...],
        *,
        all_unreferenced: bool = False,
    ) -> tuple[RetentionKey, ...]:
        """
        先执行真实退役，再模拟同一业务边界内后续失败。

        :param user_id (UUID): 原始所有者
        :param account_id (UUID): 原始账户
        :param keys (tuple[RetentionKey, ...]): 完整实际退役集合
        :param all_unreferenced (bool): 原始等待模式
        :return tuple[RetentionKey, ...]: 此注入始终抛出异常而不返回成功
        """
        await original(self, user_id, account_id, keys, all_unreferenced=all_unreferenced)
        raise SkillContentError("INJECTED_FAILURE", "failure after retirement flush")

    with monkeypatch.context() as patch:
        patch.setattr(SkillHistoryRetirementService, "retire", retire_then_fail)
        async with stopped.database.begin() as session:
            with pytest.raises(SkillContentError) as error:
                await SkillHistoryRetirementPlanner(session, SkillStoragePolicy()).apply(
                    stopped.owner, plan
                )
            assert error.value.code == "INJECTED_FAILURE"
    assert (await fingerprint(stopped))[1:] == before[1:]
    assert await preview_history(stopped, *plan.requested) == plan
