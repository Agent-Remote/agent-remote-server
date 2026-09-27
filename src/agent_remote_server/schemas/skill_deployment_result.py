"""
定义独立部署的原始 Helper 准备回执和不授予租约的结果观察。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_deployment_content import SkillDeploymentIdentity


class SkillDeploymentPreparation(BaseModel):
    """
    固定本地完整准备的归属和原输入，不代表工具已经使用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(strict=True, ge=1, le=1, description="准备回执协议版本")
    binding: SkillDeploymentIdentity = Field(description="原始完整部署绑定")
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="原完整输入规范摘要")
    directory_epoch: int = Field(strict=True, ge=1, description="原完整目录纪元")
    generation: int = Field(strict=True, ge=0, description="原配置计划代数")
    helper_receipt_id: UUID = Field(description="Helper 持久准备回执身份")


class SkillDeploymentPreparedResult(BaseModel):
    """
    把同一个持久本地准备绑定到确切领取轮次，重放不能替换任一字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    lease_attempt: int = Field(strict=True, ge=1, le=2147483647, description="原结果领取轮次")
    preparation: SkillDeploymentPreparation = Field(description="完整原始本地准备回执")


class SkillDeploymentResultObservation(BaseModel):
    """
    观察同一个结果的提交事实，不能恢复内容、执行权或过期租约。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    result: SkillDeploymentPreparedResult = Field(description="精确待确认或已确认结果")
    accepted: bool = Field(strict=True, description="是否存在完全匹配的持久结果")
    current_lease_attempt: int = Field(
        strict=True, ge=0, le=2147483647, description="锁内当前任务领取轮次"
    )
    task_status: Literal[
        "pending", "leased", "running", "succeeded", "failed", "cancelled", "expired"
    ] = Field(description="原任务当前持久阶段")
