"""
在配置受理事务内固定完整账户选择，后续读取不重新解析规则。
"""

from uuid import UUID

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_deployment import (
    DeploymentLibrarySource,
    DeploymentLocalSource,
    DeploymentSelection,
    SkillDeploymentPlan,
)
from agent_remote_server.schemas.skill_results import SkillOperationTarget
from agent_remote_server.schemas.skill_rules import SkillRuleOverride
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.skill_manager.rules import resolve_skill_rule


async def capture_plans(
    repository: SkillLibraryRepository,
    plans: SkillDeploymentRepository,
    operation: SkillOperation,
    targets: list[SkillOperationTarget],
) -> None:
    """
    对原始目标保存完整有效来源，停用项也保留原版本和启用值。

    :param repository (SkillLibraryRepository): 已持有用户锁的配置仓储
    :param plans (SkillDeploymentRepository): 同事务的追加计划仓储
    :param operation (SkillOperation): 已保存的原始操作
    :param targets (list[SkillOperationTarget]): 原始目标及待补充的摘要
    """
    for target in targets:
        account = await repository.account(operation.user_id, target.account_id)
        if account is None or account.affinity_node_id != target.node_id:
            raise ValueError("deployment target changed during locked acceptance")
        plan = await resolve_plan(repository, operation, account)
        await plans.add(plan)
        target.plan_digest = plan.digest()


async def resolve_plan(
    repository: SkillLibraryRepository, operation: SkillOperation, account: ToolAccount
) -> SkillDeploymentPlan:
    """
    只读解析指定账户，用于受理固定计划或核对原计划是否仍适用。

    :param repository (SkillLibraryRepository): 已持有用户锁的库仓储
    :param operation (SkillOperation): 原操作身份与固定代数
    :param account (ToolAccount): 已授权当前账户
    :return SkillDeploymentPlan: 当前选择在原身份下的投影，不写入历史
    """
    sources = await _library_sources(repository, operation.user_id, account.id, account.tool_type)
    for local in await repository.local.visible(operation.user_id, account.id):
        revision = next(
            (
                row
                for row in await repository.local.revisions(local)
                if row.id == local.default_revision_id
            ),
            None,
        )
        if revision is None or not revision.retained or revision.tree_digest is None:
            raise SkillContentError("REVISION_EXPIRED", "local plan input is unavailable")
        sources.append(
            DeploymentLocalSource(
                source_id=local.id,
                revision_id=revision.id,
                content_digest=revision.content_digest,
                name=local.name,
                enabled=local.enabled,
            )
        )
    plan = SkillDeploymentPlan(
        user_id=operation.user_id,
        operation_id=operation.id,
        generation=operation.generation,
        account_id=account.id,
        node_id=account.affinity_node_id,
        tool_type=account.tool_type,
        runtime_backend=account.runtime_backend,
        sources=tuple(sources),
    )
    return plan


async def _library_sources(
    repository: SkillLibraryRepository, user_id: UUID, account_id: UUID, tool_type: str
) -> list[DeploymentSelection]:
    """
    按字段继承固定每个活动库来源，账户 pin 不能被默认版本代替。

    :param repository (SkillLibraryRepository): 已授权用户仓储
    :param user_id (UUID): 当前内容所有者
    :param account_id (UUID): 精确目标账户
    :param tool_type (str): 账户实际工具类型
    :return list[DeploymentSelection]: 已固定的全部库来源
    """
    sources: list[DeploymentSelection] = []
    for item in await repository.list_installations(user_id):
        assert item.default_revision_id is not None
        tools, accounts = await repository.rules(item)
        tool = next((row for row in tools if row.tool_type == tool_type), None)
        account = next((row for row in accounts if row.account_id == account_id), None)
        rule = resolve_skill_rule(
            item.default_enabled,
            item.default_revision_id,
            SkillRuleOverride(enabled=tool.enabled, revision_id=tool.revision_id)
            if tool is not None
            else None,
            SkillRuleOverride(enabled=account.enabled, revision_id=account.revision_id)
            if account is not None
            else None,
        )
        revision = await repository.revision(user_id, item.id, str(rule.revision_id))
        if revision is None or not revision.retained or revision.tree_digest is None:
            raise SkillContentError("REVISION_EXPIRED", "library plan input is unavailable")
        sources.append(
            DeploymentLibrarySource(
                source_id=item.id,
                revision_id=revision.id,
                content_digest=revision.content_digest,
                name=item.name,
                enabled=rule.enabled,
                installation_epoch=item.epoch,
            )
        )
    return sources
