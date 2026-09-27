"""
组合整理、完整历史损失和真实分类结算规则，预计空间不依赖暂时修改数据库。
"""

from dataclasses import dataclass, replace

from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.compaction.plan import CompactionResult
from agent_remote_server.services.skills.compaction.projection import CompactionRetentionPreview
from agent_remote_server.services.skills.gc import ContentReclamationResult
from agent_remote_server.services.skills.gc.forecast import forecast_content
from agent_remote_server.services.skills.gc.planning import ContentReclamationPlan
from agent_remote_server.services.skills.retention.planning import HistoryRetirementPlan
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass(frozen=True)
class CompactionReclamationPreview:
    """
    原始整理事实、完整历史阻断、剩余树引用和预计分类结算共用一次固定分析。
    """

    retention: CompactionRetentionPreview
    retirement: HistoryRetirementPlan
    trees: tuple[StoredTreeRetention, ...]
    content: ContentReclamationPlan | None

    @property
    def ready(self) -> bool:
        """
        全部历史先可退役才有完整内容计划，不允许隐式执行无阻断子集。

        :return bool: 完整组合是否可以执行
        """
        return self.retirement.ready and self.content is not None and self.content.ready


@dataclass(frozen=True)
class CompactionReclamationResult:
    """
    一个保存点内的真实等价视图、退役身份及已登记删除任务，尚由调用方最终提交。
    """

    compaction: CompactionResult
    retired: tuple[RetentionKey, ...]
    content: ContentReclamationResult


async def forecast_reclamation(
    index: RetentionIndex,
    projected: CompactionRetentionPreview,
    retirement: HistoryRetirementPlan,
    repository: SkillContentGCRepository,
    policy: SkillStoragePolicy,
) -> CompactionReclamationPreview:
    """
    读取受影响共享摘要的所有分类，预计剩余树边中包含新等价完整树。

    :param index (RetentionIndex): 已锁定完整用户索引
    :param projected (CompactionRetentionPreview): 原计划已重验的整理投影
    :param retirement (HistoryRetirementPlan): 同次完整历史损失与阻断
    :param repository (SkillContentGCRepository): 同事务只读对象仓储
    :param policy (SkillStoragePolicy): 当前部署等待策略
    :return CompactionReclamationPreview: 不写入时钟或任务的完整预计结果
    """
    if not retirement.ready:
        return CompactionReclamationPreview(projected, retirement, (), None)
    trees, content = await forecast_content(
        index,
        retirement,
        projected.projection,
        projected.before,
        projected.after,
        projected.analyzed_at,
        repository,
        policy,
    )
    return CompactionReclamationPreview(projected, retirement, trees, content)


def same_reclamation_actions(
    actual: ContentReclamationPlan, expected: ContentReclamationPlan
) -> bool:
    """
    原时钟已在组合开始时完整重验；只允许预测时间与实际事件不同，所有释放动作必须一致。

    :param actual (ContentReclamationPlan): 整理和历史退役后同锁读到的真实计划
    :param expected (ContentReclamationPlan): 原只读预测
    :return bool: 精确树、对象边、租约、额度和阻断是否完全一致
    """
    return (
        actual.ready
        and expected.ready
        and tuple(row.key for row in actual.trees) == tuple(row.key for row in expected.trees)
        and replace(actual, trees=expected.trees) == expected
    )
