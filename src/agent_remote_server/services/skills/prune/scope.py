"""
只按已授权稳定来源和固定创建截止选择状态历史，不用内容摘要扩大范围。
"""

from datetime import datetime

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.gc.planning import utc
from agent_remote_server.services.skills.prune.plan import PruneScope
from agent_remote_server.services.skills.retention.clocks import history_records
from agent_remote_server.services.skills.retention.planning import state_history
from agent_remote_server.skill_manager.retention.graph import RetentionKey


def scoped_history(
    index: RetentionIndex, scope: PruneScope, cutoff: datetime
) -> tuple[RetentionKey, ...]:
    """
    单项含全部版本与安装纪元的检查点及迁移；其余整目录历史只能通过真实消费者关系连带。

    :param index (RetentionIndex): 已授权完整所有者索引
    :param scope (PruneScope): 已解析稳定来源范围
    :param cutoff (datetime): 原始固定分析时刻
    :return tuple[RetentionKey, ...]: 同账户、截止内的完整初始候选及退役内容证据
    """
    if scope.user_id != index.user_id:
        raise ValueError("prune scope owner mismatch")
    branches = (
        {
            row.id
            for row in index.branches
            if row.account_id == scope.account_id
            and scope.source_id in (row.installation_id, row.local_skill_id)
        }
        if scope.source_id is not None
        else set()
    )
    selected = []
    for key, row in history_records(index).items():
        if (
            not state_history(row)
            or row.account_id != scope.account_id
            or utc(row.created_at) > cutoff
        ):
            continue
        if (
            scope.source_id is None
            or isinstance(row, SkillCheckpoint)
            and row.state_id in branches
            or isinstance(row, SkillBranchPreparation)
            and (row.source_state_id in branches or row.target_state_id in branches)
        ):
            selected.append(key)
    return tuple(sorted(selected))
