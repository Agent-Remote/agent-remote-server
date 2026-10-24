"""
定义仅用户管理员使用的安全迁移观察与完整审核身份协议。
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_remote_server.schemas.config_import_requests import CanonicalUuid, canonical_uuid
from agent_remote_server.schemas.runtime_recovery import RuntimeRecoveryBinding


def nonzero_uuid(value: str) -> str:
    """
    拒绝不能用作恢复键的全零身份。

    :param value (str): 规范请求身份
    :return str: 非零原始身份
    """
    if UUID(value).int == 0:
        raise ValueError("nonzero UUID required")
    return value


def migration_task_id(value: str) -> str:
    """
    仅接受规范原迁移逻辑身份，避免回显任意调用方文本。

    :param value (str): 原始任务身份
    :return str: 未修改的规范逻辑身份
    """
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "migrate_tool_account_runtime":
        raise ValueError("canonical migration task required")
    canonical_uuid(parts[1])
    canonical_uuid(parts[2])
    return value


type RecoveryKey = Annotated[CanonicalUuid, AfterValidator(nonzero_uuid)]
type MigrationTaskId = Annotated[
    str, Field(min_length=1, max_length=128), AfterValidator(migration_task_id)
]
type RecoveryAction = Literal["verify_target", "verify_source", "repair_source"]
type RecoveryTaskStatus = Literal[
    "pending", "leased", "running", "succeeded", "failed", "cancelled", "expired"
]
type RecoveryAccountStatus = Literal[
    "binding_requested",
    "binding_session_starting",
    "binding_waiting_user_login",
    "binding_verifying",
    "active",
    "expired",
    "disabled",
    "failed",
    "node_unavailable",
    "migrating",
]


class StrictRecoveryModel(BaseModel):
    """
    拒绝未知字段和隐式类型转换。
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class VersionedRecoveryModel(StrictRecoveryModel):
    """
    强制使用整数版本一。
    """

    schema_version: Literal[1] = Field(..., description="严格整数协议版本")

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        """
        拒绝布尔或浮点被字面量等值接受。

        :param value (object): 原始版本值
        :return object: 严格整数版本
        """
        if type(value) is not int:
            raise ValueError("integer version required")
        return value


class RuntimeRecoveryOriginal(StrictRecoveryModel):
    """
    不包含内容和宿主路径的原任务完整身份投影。
    """

    task_id: MigrationTaskId = Field(..., description="规范原迁移逻辑任务")
    task_record_id: CanonicalUuid = Field(..., description="原迁移数据库记录身份")
    user_id: CanonicalUuid = Field(..., description="原账户所有者身份")
    tool_account_id: CanonicalUuid = Field(..., description="原账户身份")
    node_id: CanonicalUuid = Field(..., description="原节点身份")
    tool_type: Literal["claude"] = Field(..., description="原工具类型")
    source_runtime_backend: Literal["native", "docker_sandbox"] = Field(..., description="原后端")
    target_runtime_backend: Literal["native", "docker_sandbox"] = Field(..., description="目标后端")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """
        逻辑任务必须属于同一账户，迁移两端不能相同。

        :return Self: 已核对的完整身份
        """
        if self.task_id.split(":")[1] != self.tool_account_id:
            raise ValueError("migration account differs")
        if self.source_runtime_backend == self.target_runtime_backend:
            raise ValueError("migration backends must differ")
        return self

    @classmethod
    def from_binding(cls, binding: RuntimeRecoveryBinding) -> Self:
        """
        从已有不可变恢复绑定提取原始身份。

        :param binding (RuntimeRecoveryBinding): 保存的完整恢复绑定
        :return Self: 原迁移投影
        """
        return cls(
            task_id=binding.original_task_id,
            task_record_id=str(binding.original_task_record_id),
            user_id=str(binding.user_id),
            tool_account_id=str(binding.tool_account_id),
            node_id=str(binding.node_id),
            tool_type=binding.tool_type,
            source_runtime_backend=binding.source_runtime_backend,
            target_runtime_backend=binding.target_runtime_backend,
        )


class ReviewedRecoveryRequest(VersionedRecoveryModel):
    """
    显式动作与确认时原任务投影组成的恢复请求。
    """

    request_id: RecoveryKey = Field(..., description="调用方规范非零恢复键")
    action: RecoveryAction = Field(..., description="明确选择的恢复动作")
    expected_original: RuntimeRecoveryOriginal = Field(..., description="确认时完整原任务身份")


class RecoveryAuthorityData(VersionedRecoveryModel):
    """
    仅声明当前用户管理员身份，不授予执行权限。
    """

    actor_user_id: CanonicalUuid = Field(..., description="当前认证操作者身份")
    admin: bool = Field(..., description="当前用户是否具有管理员角色")


class RecoveryInspectionData(VersionedRecoveryModel):
    """
    原任务安全观察，不表示当前可执行或节点已恢复。
    """

    original: RuntimeRecoveryOriginal = Field(..., description="原任务完整身份")
    original_status: RecoveryTaskStatus = Field(..., description="原任务当前状态")
    account_status: RecoveryAccountStatus = Field(..., description="账户当前展示状态")


class ReviewedRecoveryData(VersionedRecoveryModel):
    """
    原始恢复绑定和其自身当前状态。
    """

    request_id: RecoveryKey = Field(..., description="原调用方恢复键")
    action: RecoveryAction = Field(..., description="原始明确恢复动作")
    binding: RuntimeRecoveryBinding = Field(..., description="原有版本化节点恢复绑定")
    status: RecoveryTaskStatus = Field(..., description="该恢复任务当前状态")


class RecoveryAuthorityResponse(StrictRecoveryModel):
    """
    当前用户权限响应。
    """

    data: RecoveryAuthorityData = Field(..., description="当前管理员权限观察")
    request_id: str | None = Field(default=None, description="请求跟踪标识")


class RecoveryInspectionResponse(StrictRecoveryModel):
    """
    无内容原任务观察响应。
    """

    data: RecoveryInspectionData = Field(..., description="原任务安全观察")
    request_id: str | None = Field(default=None, description="请求跟踪标识")


class ReviewedRecoveryResponse(StrictRecoveryModel):
    """
    新管理员恢复受理或查询响应。
    """

    data: ReviewedRecoveryData = Field(..., description="原恢复请求状态")
    request_id: str | None = Field(default=None, description="请求跟踪标识")
