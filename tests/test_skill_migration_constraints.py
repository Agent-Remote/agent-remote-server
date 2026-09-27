"""
验证增量基线与成功序号不能通过直接数据库操作跨分支或伪造成功。
"""

from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_migration import migrate, request, versions
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_preparation import SkillBranchPreparation


@pytest.mark.parametrize(
    "invalid", ["base_branch", "current_branch", "missing_sequence", "conflicted_sequence"]
)
async def test_incremental_constraints_bind_baseline_and_success(
    stopped: RuntimeHarness, tmp_path: Path, invalid: str
) -> None:
    """
    基线必须属于来源，当前侧必须属于目标，失败状态不能保存成功序号。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param invalid (str): 无效数据库更新种类
    """
    source, target = await versions(stopped, tmp_path)
    payload = await request(stopped, tmp_path, source, target)
    accepted = await migrate(stopped, tmp_path, payload)
    changes: dict[str, object]
    if invalid == "base_branch":
        changes = {"base_checkpoint_id": accepted.result_checkpoint_id}
    elif invalid == "current_branch":
        changes = {"current_checkpoint_id": payload.expected.source.checkpoint_id}
    elif invalid == "missing_sequence":
        changes = {"migration_sequence": None}
    else:
        changes = {
            "status": "conflicted",
            "result_checkpoint_id": None,
            "result_directory_id": None,
        }
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            await session.execute(
                update(SkillBranchPreparation)
                .where(SkillBranchPreparation.id == accepted.operation_id)
                .values(**changes)
            )
    async with stopped.database() as session:
        row = await session.get(SkillBranchPreparation, accepted.operation_id)
        assert row is not None and row.status == "ready" and row.migration_sequence == 1
