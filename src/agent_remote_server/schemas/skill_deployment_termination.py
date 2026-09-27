"""
固定部署撤权意图和 Helper 永久排空凭据，二者都不能单独伪造终态。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_deployment_content import SkillDeploymentIdentity

DeploymentFailureCode = Literal[
    "NODE_UNAVAILABLE",
    "TRANSFER_FAILED",
    "QUOTA_EXCEEDED",
    "DEPLOYMENT_INTERRUPTED",
    "AUTHORIZATION_DENIED",
    "SKILL_MANAGER_UNSUPPORTED",
    "DEPLOYMENT_INPUT_INVALID",
    "OPERATION_SUPERSEDED",
]


class SkillDeploymentTerminationRequest(BaseModel):
    """
    原领取只请求永久撤权，不报告 Helper 已经停止。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    lease_attempt: int = Field(strict=True, ge=1, le=2147483647, description="请求撤权的原领取轮次")
    error_code: DeploymentFailureCode = Field(description="有界原始失败原因")


class SkillDeploymentTerminationIntent(BaseModel):
    """
    Server 持久指令固定原绑定及终态分类，不能替换为当前配置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1, le=1, description="撤权指令版本")
    intent_id: UUID = Field(description="原始撤权意图身份")
    binding: SkillDeploymentIdentity = Field(description="原始完整部署绑定")
    request: SkillDeploymentTerminationRequest = Field(description="不可变原始撤权请求")
    outcome: Literal["failed", "superseded"] = Field(description="Server 固定的目标终态")
    error_code: DeploymentFailureCode = Field(description="Server 固定的目标错误码")
    retryable: bool = Field(strict=True, description="该终态是否允许精确原输入重试")


class SkillDeploymentDrain(BaseModel):
    """
    Helper 已永久封禁原尝试，不声明内容完整、Server 终态或本地清理许可。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1, le=1, description="本地排空回执版本")
    binding: SkillDeploymentIdentity = Field(description="已封禁的原始部署绑定")
    helper_receipt_id: UUID = Field(description="Helper 持久排空回执身份")


class SkillDeploymentTerminatedResult(BaseModel):
    """
    精确关联 Server 撤权和本地排空，不能靠一次普通失败释放输入。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: SkillDeploymentTerminationIntent = Field(description="精确原始持久撤权指令")
    drain: SkillDeploymentDrain = Field(description="精确原始永久排空回执")


class SkillDeploymentTerminationObservation(BaseModel):
    """
    观察原终态提案是否已提交，不返回执行授权。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    result: SkillDeploymentTerminatedResult = Field(description="精确原终态提案")
    accepted: bool = Field(strict=True, description="是否已提交相同终态结果")
    current_lease_attempt: int = Field(
        strict=True, ge=1, le=2147483647, description="当前或终态领取轮次"
    )
    task_status: Literal["pending", "leased", "running", "failed", "cancelled", "expired"] = Field(
        description="原任务持久阶段"
    )


class SkillDeploymentTerminationLookup(BaseModel):
    """
    只读获取原撤权指令，空值只表示没有已保存意图。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    intent: SkillDeploymentTerminationIntent | None = Field(description="原始撤权指令或尚未撤权")
