"""
定义固定会话快照准备阶段的精确领取轮次租约。
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class SkillSnapshotLeaseRequest(BaseModel):
    """
    本次领取序号不能替换快照或重新选择会话配置。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    lease_attempt: int = Field(ge=1, le=2147483647, strict=True, description="轮询返回的领取序号")


class SkillSnapshotLease(BaseModel):
    """
    绑定全部原始身份并返回可由调用方折算的短期时间预算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: UUID = Field(description="固定快照身份")
    task_id: UUID = Field(description="精确数据库任务身份")
    node_id: UUID = Field(description="认证节点身份")
    user_id: UUID = Field(description="原始用户身份")
    account_id: UUID = Field(description="原始工具账户身份")
    session_id: UUID = Field(description="原始工具会话身份")
    runtime_backend: Literal["native", "docker_sandbox"] = Field(description="固定运行后端")
    lease_attempt: int = Field(ge=1, le=2147483647, description="当前领取序号")
    server_time: AwareDatetime = Field(description="服务器计算授权的时间")
    lease_until: AwareDatetime = Field(description="短期任务租约截止时间")
    renew_after_milliseconds: int = Field(ge=1, description="建议下次续租前等待毫秒数")
