"""
验证保活规则作用域、完整目录上下文与新增引用的显式审计边界。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate, request, versions
from test_skill_preparation import pin
from test_skill_retention import inspect
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command

from agent_remote_server.db import Base
from agent_remote_server.models.skill_library import SkillToolOverride
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.repositories.skill_retention_schema import require_classified_schema
from agent_remote_server.skill_manager.retention.graph import RetentionGraph


@pytest.mark.parametrize("foreign_domain", [False, True])
def test_new_reference_tables_require_an_explicit_retention_policy(foreign_domain: bool) -> None:
    """
    新表即使不使用 skill 前缀，也不能通过内容外键逃过保活清单。

    :param foreign_domain (bool): 是否来自另一个领域
    """
    require_classified_schema(Base.metadata)
    metadata = MetaData()
    Table("skill_checkpoints", metadata, Column("id", Integer, primary_key=True))
    name = "future_consumer" if foreign_domain else "skill_future_history"
    Table(name, metadata, Column("checkpoint", ForeignKey("skill_checkpoints.id")))
    with pytest.raises(ValueError, match="unclassified"):
        require_classified_schema(metadata)


def test_current_directory_materialization_and_active_context_are_distinct() -> None:
    """
    当前目录本身不永久保活历史成员，完整活动上下文则保护成员检查点。
    """
    graph = RetentionGraph()
    graph.root("checkpoint", "directory", "current_directory")
    graph.edge("directory_context", "directory", "checkpoint", "directory")
    graph.edge("directory_context", "directory", "checkpoint", "old-item")
    assert not graph.protect().reasons("checkpoint", "old-item")
    graph.root("snapshot", "running", "active_snapshot")
    graph.edge("snapshot", "running", "directory_context", "directory")
    assert graph.protect().reasons("checkpoint", "old-item") == {"active_snapshot"}


async def test_shadowed_tool_pin_is_retained_without_applying_to_another_tool(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    账户选择另一版本时工具 pin 仍保护原分支，但不会扩展到不同工具账户。

    :param stopped (RuntimeHarness): 原始 Claude 账户
    :param tmp_path (Path): 私有卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    old, new = await versions(stopped, tmp_path)
    await migrate(stopped, tmp_path, await request(stopped, tmp_path, old, new))
    await pin(stopped, tmp_path, new)
    selected = (await command(stopped, tmp_path)).expected.targets[0]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    other = await library.account(tool="codex")
    other_branch = uuid4()
    async with stopped.database.begin() as session:
        session.add(
            SkillToolOverride(
                user_id=stopped.owner,
                installation_id=selected.skill_id,
                tool_type="claude",
                revision_id=old,
            )
        )
        session.add(
            AccountSkillDirectoryState(user_id=stopped.owner, account_id=other, tool_type="codex")
        )
        await session.flush()
        session.add(
            AccountSkillState(
                id=other_branch,
                user_id=stopped.owner,
                account_id=other,
                installation_id=selected.skill_id,
                installation_epoch=selected.installation_epoch,
                base_revision_id=old,
                epoch=1,
                expired=False,
            )
        )
    result = await inspect(stopped)
    assert result.reasons("branch", stopped.state) == {"pin"}
    assert selected.state_id is not None
    assert "current_branch" in result.reasons("branch", selected.state_id)
    assert not result.reasons("branch", other_branch)
