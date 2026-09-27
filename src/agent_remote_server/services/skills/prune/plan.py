"""
固定账户候选、完整阻断、可执行依赖组与内容预测，不承担公开回执语义。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from agent_remote_server.services.skills.compaction.plan import CompactionPlan, CompactionResult
from agent_remote_server.services.skills.gc import ContentReclamationResult
from agent_remote_server.services.skills.gc.planning import ContentReclamationPlan
from agent_remote_server.services.skills.retention.history import HistoryRetention
from agent_remote_server.services.skills.retention.planning import HistoryRetirementPlan
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.skill_manager.retention.graph import RetentionKey, RetentionProtection
from agent_remote_server.skill_manager.retention.groups import HistoryGroups
from agent_remote_server.skill_manager.retention.projection import RetentionProjection


@dataclass(frozen=True)
class PruneScope:
    """
    规范范围只包含已授权所有者、账户和稳定来源身份；来源为空表示完整账户目录。
    """

    user_id: UUID
    account_id: UUID
    source_id: UUID | None


@dataclass(frozen=True)
class PrunePlan:
    """
    一次固定分析的完整可审阅动作和阻断，执行阶段不能再选择容易成功的子集。
    """

    scope: PruneScope
    cutoff: datetime
    all_unreferenced: bool
    library_generation: int
    compaction: CompactionPlan | None
    projection: RetentionProjection
    before: RetentionProtection
    after: RetentionProtection
    history: tuple[HistoryRetention, ...]
    candidates: HistoryRetirementPlan | None
    selection: HistoryGroups
    retirement: HistoryRetirementPlan | None
    trees: tuple[StoredTreeRetention, ...]
    content: ContentReclamationPlan
    claims: tuple[tuple[UUID, str, str, str], ...]


@dataclass(frozen=True)
class PruneResult:
    """
    原子业务提交中的整理、实际退役与结算结果；文件仍由提交后的独立 worker 删除。
    """

    compaction: CompactionResult | None
    retired: tuple[RetentionKey, ...]
    content: ContentReclamationResult
