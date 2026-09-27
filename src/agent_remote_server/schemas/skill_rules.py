"""
定义技能覆盖规则及其可解释的解析结果。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

type SkillRuleScope = Literal["user", "tool", "account"]
type SkillExclusionReason = Literal["removed", "unsupported_tool", "excluded_incompatible"]


class SkillRuleOverride(BaseModel):
    """
    独立覆盖启用状态和版本的工具或账户规则。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool | None = Field(default=None, strict=True, description="启用覆盖，空值表示继承")
    revision_id: UUID | None = Field(default=None, description="固定版本，空值表示继承")


class ResolvedSkillRule(BaseModel):
    """
    包含各字段来源和实际可用资格的技能规则。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(description="解析后的启用意图")
    revision_id: UUID = Field(description="解析后的固定版本")
    enabled_source: SkillRuleScope = Field(description="启用状态的来源层级")
    revision_source: SkillRuleScope = Field(description="版本选择的来源层级")
    eligible: bool = Field(description="是否满足工具和安装前置条件")
    included: bool = Field(description="是否实际纳入新会话")
    exclusion_reason: SkillExclusionReason | None = Field(description="无法纳入的前置条件原因")
