"""
验证用户、工具和账户规则按字段继承。
"""

from uuid import UUID

import pytest

from agent_remote_server.schemas.skill_rules import SkillRuleOverride
from agent_remote_server.skill_manager.rules import resolve_skill_rule


@pytest.mark.parametrize(
    ("default", "tool", "account", "expected", "source"),
    [
        (True, None, None, True, "user"),
        (False, None, None, False, "user"),
        (True, False, None, False, "tool"),
        (False, True, None, True, "tool"),
        (True, False, True, True, "account"),
        (False, True, False, False, "account"),
    ],
)
def test_enabled_inheritance_preserves_explicit_false(
    default: bool,
    tool: bool | None,
    account: bool | None,
    expected: bool,
    source: str,
) -> None:
    """
    显式关闭不得被当作缺少覆盖值。

    :param default (bool): 用户默认状态
    :param tool (bool | None): 工具覆盖状态
    :param account (bool | None): 账户覆盖状态
    :param expected (bool): 预期解析结果
    :param source (str): 预期来源层级
    """
    result = resolve_skill_rule(
        default,
        UUID(int=1),
        SkillRuleOverride(enabled=tool),
        SkillRuleOverride(enabled=account),
    )
    assert result.enabled is expected
    assert result.enabled_source == source
    assert result.included is expected


def test_version_pin_and_enabled_resolve_independently() -> None:
    """
    账户固定版本可以独立继承工具的停用状态。
    """
    result = resolve_skill_rule(
        True,
        UUID(int=3),
        SkillRuleOverride(enabled=False, revision_id=UUID(int=2)),
        SkillRuleOverride(revision_id=UUID(int=1)),
    )
    assert result.enabled is False
    assert result.enabled_source == "tool"
    assert result.revision_id == UUID(int=1)
    assert result.revision_source == "account"


def test_inherit_restores_parent_pin_and_exclusion_is_not_overridable() -> None:
    """
    恢复继承后使用上级版本且账户不能绕过卸载前置条件。
    """
    result = resolve_skill_rule(
        True,
        UUID(int=3),
        SkillRuleOverride(revision_id=UUID(int=2)),
        SkillRuleOverride(enabled=True),
        exclusion_reason="removed",
    )
    assert result.revision_id == UUID(int=2)
    assert result.revision_source == "tool"
    assert result.enabled is True
    assert result.eligible is False
    assert result.included is False
    assert result.exclusion_reason == "removed"
