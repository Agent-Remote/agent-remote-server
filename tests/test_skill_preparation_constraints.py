"""
验证准备引用无法跨归属、分支或目录范围，缺少来源纪元也不能入库。
"""

from pathlib import Path
from uuid import UUID

import pytest
from skill_runtime_support import RuntimeHarness, runtime
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_preparation import prepare, request, update_version
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_preparation import SkillBranchPreparation


@pytest.mark.parametrize(
    "invalid", ["source_owner", "source_checkpoint", "directory_scope", "source_epoch"]
)
async def test_preparation_database_constraints_preserve_exact_ownership(
    stopped: RuntimeHarness, tmp_path: Path, invalid: str
) -> None:
    """
    直接数据库写入也不能把准备输入绑定到错误来源或把成员冒充目录。

    :param stopped (RuntimeHarness): 原始已使用账户
    :param tmp_path (Path): 内容卷
    :param invalid (str): 无效归属或范围种类
    """
    await update_version(stopped, tmp_path, "next")
    accepted = await prepare(stopped, tmp_path, await request(stopped, tmp_path))
    changed: dict[str, UUID | None]
    if invalid == "source_owner":
        other = await runtime(stopped.database, tmp_path / "other")
        changed = {"source_state_id": other.state, "source_checkpoint_id": other.item}
    elif invalid == "source_checkpoint":
        changed = {"source_checkpoint_id": accepted.result_checkpoint_id}
    elif invalid == "directory_scope":
        changed = {"result_directory_id": accepted.result_checkpoint_id}
    else:
        changed = {"source_epoch": None}
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            await session.execute(
                update(SkillBranchPreparation)
                .where(SkillBranchPreparation.id == accepted.operation_id)
                .values(**changed)
            )
    async with stopped.database() as session:
        original = await session.get(SkillBranchPreparation, accepted.operation_id)
        assert original is not None and original.source_epoch == accepted.source_epoch
        assert original.result_directory_id == accepted.result_directory_id
