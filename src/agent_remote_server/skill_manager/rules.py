"""
实现不依赖数据库的技能字段继承规则。
"""

from uuid import UUID

from agent_remote_server.schemas.skill_rules import (
    ResolvedSkillRule,
    SkillExclusionReason,
    SkillRuleOverride,
    SkillRuleScope,
)


def resolve_skill_rule(
    default_enabled: bool,
    default_revision_id: UUID,
    tool_rule: SkillRuleOverride | None = None,
    account_rule: SkillRuleOverride | None = None,
    *,
    exclusion_reason: SkillExclusionReason | None = None,
) -> ResolvedSkillRule:
    """
    按账户、工具、用户默认顺序独立解析两个字段。

    :param default_enabled (bool): 用户默认启用状态
    :param default_revision_id (UUID): 用户默认版本标识
    :param tool_rule (SkillRuleOverride | None): 工具覆盖规则
    :param account_rule (SkillRuleOverride | None): 账户覆盖规则
    :param exclusion_reason (SkillExclusionReason | None): 无法覆盖的前置条件失败
    :return ResolvedSkillRule: 可解释的有效规则
    """
    enabled = default_enabled
    revision_id = default_revision_id
    enabled_source: SkillRuleScope = "user"
    revision_source: SkillRuleScope = "user"
    overrides: tuple[tuple[SkillRuleScope, SkillRuleOverride | None], ...] = (
        ("tool", tool_rule),
        ("account", account_rule),
    )
    for scope, rule in overrides:
        if rule is None:
            continue
        if rule.enabled is not None:
            enabled = rule.enabled
            enabled_source = scope
        if rule.revision_id is not None:
            revision_id = rule.revision_id
            revision_source = scope
    return ResolvedSkillRule(
        enabled=enabled,
        revision_id=revision_id,
        enabled_source=enabled_source,
        revision_source=revision_source,
        eligible=exclusion_reason is None,
        included=enabled and exclusion_reason is None,
        exclusion_reason=exclusion_reason,
    )
