"""
定义不携带账户内容或宿主路径的显式迁移恢复协议。
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def _absent(value: object) -> bool:
    """
    省略可选动作以保持旧协议字段集合。

    :param value (object): 待序列化的字段值
    :return bool: 是否省略该字段
    """
    return value is None


class RuntimeRecoveryRequest(BaseModel):
    """
    管理员指定原逻辑任务及独立请求键。
    """

    model_config = ConfigDict(extra="forbid")
    original_task_id: str = Field(..., min_length=1, max_length=128, description="原迁移逻辑任务")
    request_id: UUID = Field(..., description="独立恢复幂等键")
    action: Literal["verify_source", "repair_source"] | None = Field(
        default=None, exclude_if=_absent, description="显式验证或修复源后端权限"
    )

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        """
        默认动作仅由字段缺失表达，拒绝显式空值。

        :return Self: 已验证请求
        """
        if "action" in self.model_fields_set and self.action is None:
            raise ValueError("Recovery action must be omitted, verify_source or repair_source")
        return self


class RuntimeRecoveryBinding(BaseModel):
    """
    服务端固定的原迁移和恢复任务完整身份。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2, 3] = Field(default=1, description="协议版本")
    task_id: str = Field(..., description="恢复逻辑任务")
    task_record_id: UUID = Field(..., description="恢复任务记录")
    original_task_id: str = Field(..., description="原迁移逻辑任务")
    original_task_record_id: UUID = Field(..., description="原迁移任务记录")
    node_id: UUID = Field(..., description="原节点")
    user_id: UUID = Field(..., description="账户所有者")
    tool_account_id: UUID = Field(..., description="原账户")
    tool_type: Literal["claude"] = Field(default="claude", description="工具类型")
    source_runtime_backend: Literal["native", "docker_sandbox"] = Field(..., description="原后端")
    target_runtime_backend: Literal["native", "docker_sandbox"] = Field(..., description="目标后端")

    action: Literal["verify_source", "repair_source"] | None = Field(
        default=None, exclude_if=_absent, description="版本二源验证或版本三源修复动作"
    )

    @model_validator(mode="after")
    def validate_version_action(self) -> Self:
        """
        保持版本一原始字段集合，版本二和三必须分别选择源验证或修复。

        :return Self: 已验证完整绑定
        """
        if self.version == 1:
            if "action" in self.model_fields_set:
                raise ValueError("Version one cannot carry an action")
        elif (self.version, self.action) not in {(2, "verify_source"), (3, "repair_source")}:
            raise ValueError("Recovery version and source action differ")
        return self


class RuntimeRecoveryAuthorization(BaseModel):
    """
    当前活动租约的原始恢复绑定。
    """

    model_config = ConfigDict(extra="forbid")
    binding: RuntimeRecoveryBinding = Field(..., description="不可变恢复身份")
    lease_attempt: int = Field(..., gt=0, strict=True, description="当前轮询次数")


class RuntimeRecoveryLease(BaseModel):
    """
    原始授权的短期续租结果，不改变恢复任务或账户权限。
    """

    authorization: RuntimeRecoveryAuthorization = Field(..., description="原始恢复授权")
    server_time: AwareDatetime = Field(..., description="本次授权计算时间")
    lease_until: AwareDatetime = Field(..., description="本次活动租约截止时间")
    renew_after_milliseconds: int = Field(..., gt=0, description="建议续租间隔毫秒数")


class RuntimeRecoveryLeaseResponse(BaseModel):
    """
    节点续租结果响应，不携带私有内容。
    """

    data: RuntimeRecoveryLease = Field(..., description="原始任务的短期租约")
    request_id: str | None = Field(default=None, description="请求跟踪标识")


class RuntimeRecoveryData(BaseModel):
    """
    独立恢复任务当前状态，不把受理误报为已恢复。
    """

    binding: RuntimeRecoveryBinding = Field(..., description="不可变恢复身份")
    status: str = Field(..., description="恢复任务状态")


class RuntimeRecoveryResponse(BaseModel):
    """
    管理员受理或查询响应。
    """

    data: RuntimeRecoveryData = Field(..., description="恢复任务")
    request_id: str | None = Field(default=None, description="请求跟踪标识")


class RuntimeRecoveryAuthorizationResponse(BaseModel):
    """
    原节点即时检查响应。
    """

    data: RuntimeRecoveryAuthorization = Field(..., description="当前租约授权")
    request_id: str | None = Field(default=None, description="请求跟踪标识")
