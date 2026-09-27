"""
共享草稿与完整解决的候选覆盖说明，操作状态和是否发布由调用方分别表达。
"""

from uuid import UUID

from agent_remote_server.schemas.skill_migration_conflicts import SkillMigrationConflictView
from agent_remote_server.schemas.skill_migration_resolution import (
    SkillMigrationResolutionDetails,
    SkillMigrationResolutionView,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.migration_resolution_plan import (
    MigrationResolutionCalculation,
)
from agent_remote_server.skill_manager.manifest import manifest_digest


def resolution_details(
    calculation: MigrationResolutionCalculation, operation_id: UUID | None, revision: int
) -> SkillMigrationResolutionDetails:
    """
    从完整计算生成稳定响应字段，不重新加载输入或解释今天的规则。

    :param calculation (MigrationResolutionCalculation): 已验证的原始候选及全部差异
    :param operation_id (UUID | None): 操作身份，预览为空
    :param revision (int): 当前或已保存计划版本
    :return SkillMigrationResolutionDetails: 不声称发布完成的公共说明
    """
    return SkillMigrationResolutionDetails(
        migration_id=calculation.info.id,
        operation_id=operation_id,
        plan_revision=revision,
        choices=list(calculation.choices),
        candidate_complete=calculation.result.merged is not None,
        remaining=calculation.result.conflicts,
        unit=calculation.unit,
        result_tree_digest=manifest_digest(calculation.result.merged)
        if calculation.result.merged is not None
        else None,
        target_revision_id=calculation.target_revision_id,
        target_modified=calculation.target_modified,
        target_changes=calculation.target_changes,
        original_changes=calculation.original_changes,
        directory_changes=calculation.directory_changes,
        other_changed_roots=calculation.other_changed_roots,
    )


def stale_resolution_view(
    info: SkillMigrationConflictView,
    choices: list[SkillResolutionChoice],
    revision: int,
    operation_id: UUID | None,
    reasons: tuple[str, ...],
    recompute: bool,
    replacement_id: UUID | None,
) -> SkillMigrationResolutionView:
    """
    过期响应保留原选择摘要但不应用请求，替代状态不能冒充原操作成功。

    :param info (SkillMigrationConflictView): 原始冲突及实时诊断
    :param choices (list[SkillResolutionChoice]): 仅用于说明的旧计划
    :param revision (int): 未改变的旧计划版本
    :param operation_id (UUID | None): 本次受理身份，预览为空
    :param reasons (tuple[str, ...]): 过期或终止原因
    :param recompute (bool): 是否可按同纪元重新比较
    :param replacement_id (UUID | None): 已保存替代身份
    :return SkillMigrationResolutionView: 原尝试的预览或失效回执
    """
    return SkillMigrationResolutionView(
        migration_id=info.id,
        operation_id=operation_id,
        plan_revision=revision,
        choices=choices,
        candidate_complete=False,
        remaining=info.original.conflicts,
        unit=tuple(
            sorted(
                {info.name}
                | {name for conflict in info.original.conflicts for name in conflict.unit}
            )
        ),
        result_tree_digest=None,
        target_revision_id=info.live.target.revision_id,
        target_modified=None,
        target_changes=None,
        original_changes=None,
        directory_changes=None,
        other_changed_roots=(),
        status="preview" if operation_id is None else "superseded",
        result_checkpoint_id=None,
        result_directory_id=None,
        migration_sequence=None,
        affected=[],
        replacement_id=replacement_id,
        stale_reasons=reasons,
        recomputation_possible=recompute,
    )
