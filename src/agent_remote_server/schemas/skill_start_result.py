"""
定义受管 Native 启动结果的精确身份和不含内容的运行字段。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ManagedStartIdentity(BaseModel):
    """
    结果不能替换快照、会话或任务领取轮次。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: UUID = Field(description="原始会话身份")
    tool_account_id: UUID = Field(description="原始账户身份")
    runtime_backend: Literal["native"] = Field(description="已验证的运行后端")
    runtime_resource_id: str = Field(max_length=128, strict=True, description="原始中立资源名")
    skill_snapshot_id: UUID = Field(description="固定会话快照")
    task_record_id: UUID = Field(description="精确数据库任务身份")
    lease_attempt: int = Field(ge=1, le=2147483647, strict=True, description="当前任务领取轮次")


class ManagedStartReady(ManagedStartIdentity):
    """
    原始工具运行已就绪，不携带宿主路径、UID 或凭据。
    """

    status: Literal["running"] = Field(description="Helper 证明的就绪结果")
    tool_type: Literal["claude"] = Field(description="原始工具类型")
    tmux_session_name: str = Field(
        min_length=1, max_length=128, strict=True, description="原始终端名称"
    )


class ManagedStartStopped(ManagedStartIdentity):
    """
    原始启动已经停止，技能副本仍须独立收尾。
    """

    code: Literal["SKILL_START_STOPPED"] = Field(description="已停止启动的稳定错误码")
    message: Literal["Managed startup stopped; skill finalization is pending."] = Field(
        description="不含运行内容的固定说明"
    )


class ManagedStartObservation(BaseModel):
    """
    只读观察原始回报是否已提交，不授予执行或租约权限。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    result: ManagedStartReady | ManagedStartStopped = Field(description="精确原始待确认结果")
    accepted: bool = Field(strict=True, description="是否存在完全匹配的已提交收据")
    current_lease_attempt: int = Field(
        ge=0, le=2147483647, strict=True, description="锁内观察到的当前领取轮次"
    )
    task_status: Literal[
        "pending", "leased", "running", "succeeded", "failed", "cancelled", "expired"
    ] = Field(description="锁内观察到的原始任务状态")
