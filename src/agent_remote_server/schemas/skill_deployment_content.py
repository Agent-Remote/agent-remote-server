"""
定义独立部署的完整输入与短期租约封套，不借用会话身份。
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


class SkillDeploymentIdentity(BaseModel):
    """
    固定原始目标、尝试、任务和完整目录，摘要不单独构成授权。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID = Field(description="原配置操作身份")
    attempt_id: UUID = Field(description="精确部署尝试身份")
    task_id: UUID = Field(description="精确任务数据库身份")
    user_id: UUID = Field(description="原始内容所有者")
    account_id: UUID = Field(description="原始目标账户")
    node_id: UUID = Field(description="原始执行节点")
    checkpoint_id: UUID = Field(description="独立完整目录检查点")
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="原配置计划摘要")
    tree_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="原完整目录摘要")
    runtime_backend: Literal["native"] = Field(description="已支持的固定部署后端")


class SkillDeploymentMember(BaseModel):
    """
    独立部署输入固定原分支和检查点，不记录工具已经使用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="原物化条目名称")
    state_id: UUID = Field(description="原账户分支")
    state_epoch: int = Field(strict=True, ge=1, description="原分支纪元")
    checkpoint_id: UUID = Field(description="原分支完整检查点")


class SkillDeploymentContent(SkillDeploymentIdentity):
    """
    原始配置与完整物化目录同时返回，清单可用不等于执行就绪。
    """

    directory_epoch: int = Field(strict=True, ge=1, description="原目录纪元")
    plan: SkillDeploymentPlan = Field(description="受理时保存的完整原计划")
    manifest: SkillTreeManifest = Field(description="原完整目录清单")
    items: tuple[SkillDeploymentMember, ...] = Field(description="原物化分支完整集合")


class SkillDeploymentLeaseRequest(BaseModel):
    """
    只能续期原任务当前领取轮次，不选择另一个输入。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    lease_attempt: int = Field(strict=True, ge=1, le=2147483647, description="当前领取轮次")


class SkillDeploymentLease(SkillDeploymentIdentity):
    """
    服务器短期相对预算不依赖 Node 与 Server 的墙上时间一致。
    """

    lease_attempt: int = Field(strict=True, ge=1, le=2147483647, description="当前领取轮次")
    server_time: AwareDatetime = Field(description="本次服务器时间")
    lease_until: AwareDatetime = Field(description="续期后的短期截止时间")
    renew_after_milliseconds: int = Field(strict=True, ge=1, description="建议下次续租间隔毫秒")
