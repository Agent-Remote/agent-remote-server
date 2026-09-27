"""
重试只选择原操作的精确失败尝试，不允许提交新来源或新计划。
"""

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillRetryTarget(BaseModel):
    """
    固定原账户和调用方已观察到的终态尝试。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: UUID = Field(description="原始目标账户")
    attempt_id: UUID = Field(description="期望恢复的当前失败尝试")


class SkillDeploymentRetryRequest(BaseModel):
    """
    相同幂等键只接受相同原操作代数和同一组失败目标。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(min_length=1, max_length=128, description="持久化重试请求键")
    expected_generation: int = Field(
        strict=True, ge=0, description="原操作配置代数，不是当前库代数"
    )
    targets: tuple[SkillRetryTarget, ...] = Field(
        min_length=1, max_length=10000, description="全部明确选定的原始失败尝试"
    )

    @model_validator(mode="after")
    def distinct_targets(self) -> Self:
        """
        每个原目标只恢复一次，重复输入不能产生分叉尝试。

        :return Self: 账户身份唯一的请求
        """
        if len({target.account_id for target in self.targets}) != len(self.targets):
            raise ValueError("duplicate deployment retry target")
        return self
