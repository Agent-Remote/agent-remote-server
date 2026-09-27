"""
验证迁移替代链的数据库归属、单后继和失效状态约束。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness, runtime
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_migration import migrate, request
from test_skill_migration_conflicts import pending
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.schemas.skill_migration import SkillMigrationView


@pytest.mark.parametrize(
    "invalid",
    [
        "self-parent",
        "self-replacement",
        "active-replacement",
        "active-reason",
        "missing-parent",
        "duplicate-child",
        "foreign-parent",
        "foreign-replacement",
    ],
)
async def test_replacement_links_enforce_scope_status_and_single_child(
    stopped: RuntimeHarness, tmp_path: Path, invalid: str
) -> None:
    """
    即使绕过服务写 SQL，也不能伪造活跃替代、跨用户引用或一个旧尝试的多个子比较。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param invalid (str): 违反的持久化约束
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView) and original.operation_id is not None
    identity = original.operation_id
    second = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped,
            tmp_path,
            original.before.source.revision_id,
            original.before.target.revision_id,
        ),
    )
    assert second.operation_id is not None
    values: dict[str, object]
    if invalid == "self-parent":
        values = {"recomputed_from_id": identity}
    elif invalid == "self-replacement":
        values = {"status": "superseded", "replacement_id": identity}
    elif invalid == "active-replacement":
        values = {"replacement_id": second.operation_id}
    elif invalid == "active-reason":
        values = {"superseded_reason": "target_changed"}
    elif invalid == "missing-parent":
        values = {"recomputed_from_id": uuid4()}
    elif invalid == "duplicate-child":
        async with stopped.database.begin() as session:
            await session.execute(
                update(SkillBranchPreparation)
                .where(SkillBranchPreparation.id == second.operation_id)
                .values(recomputed_from_id=identity)
            )
        third = await migrate(
            stopped,
            tmp_path,
            await request(
                stopped,
                tmp_path,
                original.before.source.revision_id,
                original.before.target.revision_id,
            ),
        )
        assert third.operation_id is not None
        identity = third.operation_id
        values = {"recomputed_from_id": original.operation_id}
    else:
        other = await runtime(stopped.database, tmp_path / "other")
        async with stopped.database.begin() as session:
            row = await session.get(SkillBranchPreparation, original.operation_id)
            assert row is not None
            branch = await session.get(AccountSkillState, other.state)
            assert branch is not None
            foreign = SkillBranchPreparation(
                id=uuid4(),
                user_id=other.owner,
                account_id=other.account,
                installation_id=branch.installation_id,
                installation_epoch=branch.installation_epoch,
                idempotency_key=str(uuid4()),
                request_digest="0" * 64,
                target_state_id=other.state,
                target_epoch=1,
                directory_epoch=1,
                library_generation=1,
                directory_checkpoint_id=other.directory,
                base_digest=other.tree,
                current_digest=other.tree,
                incoming_digest=other.tree,
                mode="initial",
                status="conflicted",
                response_json=row.response_json,
            )
            session.add(foreign)
            await session.flush()
            foreign_id = foreign.id
        values = (
            {"recomputed_from_id": foreign_id}
            if invalid == "foreign-parent"
            else {"status": "superseded", "replacement_id": foreign_id}
        )
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            await session.execute(
                update(SkillBranchPreparation)
                .where(SkillBranchPreparation.id == identity)
                .values(**values)
            )
    async with stopped.database() as session:
        row = await session.get(SkillBranchPreparation, identity)
        assert row is not None and row.status == "conflicted" and row.replacement_id is None
