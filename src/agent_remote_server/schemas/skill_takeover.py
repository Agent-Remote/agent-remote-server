"""
定义首次接管的比较前置条件、旧资源清单与节点捕获声明。
"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


class SkillTakeoverRequest(BaseModel):
    """
    用户只指定已授权账户的旧纪元，节点和目录均由 Server 选择。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="持久化幂等键"
    )
    expected_directory_epoch: int = Field(
        ge=0, lt=2**63 - 1, strict=True, description="旧目录纪元，无记录时为零"
    )


class SkillTakeoverLeaseRequest(BaseModel):
    """
    只续期本次领取轮次，不允许旧进程借另一轮次恢复授权。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    lease_attempt: int = Field(ge=1, le=2147483647, strict=True, description="轮询返回的领取序号")


class SkillTakeoverLease(BaseModel):
    """
    返回相对服务器时间的短期授权，不改变持久捕获身份。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    takeover_id: UUID = Field(description="原始接管身份")
    task_id: UUID = Field(description="精确数据库任务身份")
    node_id: UUID = Field(description="认证节点身份")
    lease_attempt: int = Field(ge=1, le=2147483647, description="当前领取序号")
    server_time: AwareDatetime = Field(description="服务器计算本次授权的时间")
    lease_until: AwareDatetime = Field(description="本次短期任务租约截止时间")
    renew_after_milliseconds: int = Field(ge=1, description="建议下次续租前等待毫秒数")


class SkillTakeoverWriter(BaseModel):
    """
    只保留可核实的资源身份，不保存配置、文件字节或宿主路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["session", "binding", "import", "backend"] = Field(
        description="可能写入旧目录的资源类型"
    )
    node_id: UUID = Field(description="原执行节点")
    resource_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]+$",
        description="原会话、绑定或任务身份",
    )
    runtime_backend: Literal["native", "docker_sandbox"] | None = Field(
        default=None, description="原后端，未知时不能以当前后端猜测"
    )
    task_id: UUID | None = Field(default=None, description="仍可定位的原数据库任务身份")


class SkillTakeoverCapture(BaseModel):
    """
    声明来自 Helper 的稳定冻结输入，不能把任务终态当作本地静止证明。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    helper_receipt_id: UUID = Field(description="Helper 持久化冻结收据身份")
    directory_epoch: int = Field(ge=1, strict=True, description="被围栏固定的目录纪元")
    inventory_digest: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="Server 固定的旧资源清单摘要"
    )
    writers_quiescent: bool = Field(
        strict=True, description="Helper 已核实全部旧写入者退出，必须为真"
    )
    manifest: SkillTreeManifest = Field(description="排除系统技能后的完整冻结目录")


class SkillTakeoverView(BaseModel):
    """
    仅返回精确任务的固定清单与原始提交收据，不包含文件或宿主路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = Field(default=1, description="接管协议版本")
    manifest_version: Literal[1] = Field(default=1, description="完整树协议版本")
    takeover_id: UUID = Field(description="原始接管身份")
    task_id: UUID = Field(description="精确数据库任务身份")
    node_id: UUID = Field(description="认证节点身份")
    user_id: UUID = Field(description="原始活动所有者")
    account_id: UUID = Field(description="原始账户身份")
    runtime_backend: Literal["native", "docker_sandbox"] = Field(description="固定运行后端")
    directory_epoch: int = Field(ge=1, description="原始接管纪元")
    inventory_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="不可变清单摘要")
    inventory: list[SkillTakeoverWriter] = Field(max_length=10000, description="固定旧写入者清单")
    status: Literal["reserved", "uploading", "committed"] = Field(description="持久接管阶段")
    helper_receipt_id: UUID | None = Field(description="Helper 固定捕获身份")
    capture_digest: str | None = Field(description="原始完整捕获摘要")
    upload_id: UUID | None = Field(description="当前上传尝试身份")
    upload_attempt: int = Field(ge=0, description="已预约上传尝试次数")
    checkpoint_id: UUID | None = Field(description="原始权威提交的完整目录检查点")
