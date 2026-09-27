"""
比较变更前后的实际启用选择，只向需要更新目录的账户部署。
"""

from uuid import UUID

from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_rules import SkillRuleOverride
from agent_remote_server.skill_manager.rules import resolve_skill_rule

type Selection = frozenset[tuple[str, UUID, int, UUID | None]]


async def account_selections(
    repository: SkillLibraryRepository, user_id: UUID
) -> dict[UUID, Selection]:
    """
    保存不可变选择值，禁用项和字段来源变化不触发部署，移除旧启用项仍触发。

    :param repository (SkillLibraryRepository): 已持有用户锁的库仓储
    :param user_id (UUID): 已认证内容所有者
    :return dict[UUID, Selection]: 每个账户当前实际启用的来源、纪元和版本
    """
    accounts = await repository.accounts(user_id)
    selected: dict[UUID, set[tuple[str, UUID, int, UUID | None]]] = {
        account.id: set() for account in accounts
    }
    for item in await repository.list_installations(user_id):
        assert item.default_revision_id is not None
        tools, overrides = await repository.rules(item)
        tool_rules = {
            row.tool_type: SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
            for row in tools
        }
        account_rules = {
            row.account_id: SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
            for row in overrides
        }
        for account in accounts:
            rule = resolve_skill_rule(
                item.default_enabled,
                item.default_revision_id,
                tool_rules.get(account.tool_type),
                account_rules.get(account.id),
            )
            if rule.enabled:
                selected[account.id].add(("library", item.id, item.epoch, rule.revision_id))
    for account in accounts:
        for local in await repository.local.visible(user_id, account.id):
            if local.enabled:
                selected[account.id].add(("local", local.id, 0, local.default_revision_id))
    return {account_id: frozenset(values) for account_id, values in selected.items()}
