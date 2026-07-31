from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from agent_remote_server.device_control_limits import (
    MAX_ACTIVE_DEVICE_SESSION_GENERATION,
    MAX_DEVICE_SESSION_GENERATION,
)

DeviceSessionStatus = Literal[
    "pending_device",
    "pending_user_approval",
    "active",
    "stopping",
    "stopped",
    "denied",
    "expired",
    "failed",
]
ControlLevel = Literal["view_only", "click_only", "full_control"]


class CreateDeviceSessionRequest(BaseModel):
    """
    创建设备控制会话请求
    """

    device_id: UUID = Field(..., description="待控制的本机设备 ID")
    tool_session_id: UUID = Field(..., description="绑定的远端 Claude session ID")


class DeviceSessionData(BaseModel):
    """
    设备控制会话响应数据
    """

    id: UUID = Field(..., description="设备控制会话 ID")
    user_id: UUID = Field(..., description="所属用户 ID")
    device_id: UUID = Field(..., description="被控制设备 ID")
    tool_session_id: UUID = Field(..., description="远端工具 session ID")
    node_id: UUID = Field(..., description="远端 Node ID")
    platform: Literal["macos"] = Field(..., description="设备平台")
    status: DeviceSessionStatus = Field(..., description="设备控制状态")
    generation: int = Field(
        ..., ge=1, le=MAX_DEVICE_SESSION_GENERATION, description="连接与撤销代次"
    )
    lease_until: datetime | None = Field(default=None, description="当前短期租约到期时间")
    expires_at: datetime = Field(..., description="会话最晚到期时间")
    lock_acquired_at: datetime | None = Field(default=None, description="机器锁获取时间")
    stopped_at: datetime | None = Field(default=None, description="停止时间")
    stop_reason: str | None = Field(default=None, description="不含敏感内容的停止原因")
    created_at: datetime = Field(..., description="创建时间")


class DeviceSessionResponse(BaseModel):
    """
    设备控制会话响应
    """

    data: DeviceSessionData = Field(..., description="设备控制会话数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class DeviceSessionListData(BaseModel):
    """
    设备控制会话列表数据
    """

    items: list[DeviceSessionData] = Field(default_factory=list, description="设备控制会话列表")


class DeviceSessionListResponse(BaseModel):
    """
    设备控制会话列表响应
    """

    data: DeviceSessionListData = Field(..., description="设备控制会话列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class DeviceApprovalItem(BaseModel):
    """
    本机应用审批摘要
    """

    application_digest: str = Field(
        ..., min_length=64, max_length=64, description="应用稳定标识 SHA-256 摘要"
    )
    control_level: ControlLevel = Field(..., description="本机展示并批准的控制等级")
    approval_result: Literal["allowed", "denied"] = Field(..., description="本机审批结果")
    clipboard_allowed: bool = Field(default=False, description="是否额外批准剪贴板权限")

    @field_validator("application_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        """
        校验应用标识摘要为小写十六进制 SHA-256

        :param value (str): 待校验的应用标识摘要

        :return str: 已校验的应用标识摘要
        """

        if any(character not in "0123456789abcdef" for character in value):
            raise ValueError("application digest must be lowercase hexadecimal")
        return value


class ApproveDeviceSessionRequest(BaseModel):
    """
    本机批准设备控制会话请求
    """

    generation: int = Field(
        ..., ge=1, le=MAX_ACTIVE_DEVICE_SESSION_GENERATION, description="本机当前连接代次"
    )
    approvals: list[DeviceApprovalItem] = Field(
        ..., min_length=1, max_length=32, description="本机应用审批摘要"
    )


class RenewDeviceSessionRequest(BaseModel):
    """
    续租设备控制会话请求
    """

    generation: int = Field(
        ..., ge=1, le=MAX_ACTIVE_DEVICE_SESSION_GENERATION, description="待续租连接代次"
    )


class StopDeviceSessionRequest(BaseModel):
    """
    停止设备控制会话请求
    """

    reason: Literal["user_stop", "session_end", "lease_expired", "failed"] = Field(
        ..., description="不含敏感内容的停止原因"
    )


class AbortDeviceActionRequest(BaseModel):
    """
    本机中止当前设备动作请求
    """

    generation: int = Field(
        ...,
        ge=1,
        le=MAX_ACTIVE_DEVICE_SESSION_GENERATION,
        description="被中止动作所在连接代次",
    )
    reason: Literal["esc", "local_stop", "disconnect"] = Field(
        ..., description="不含敏感内容的中止原因"
    )


class RegisterDeviceRelayMaterialRequest(BaseModel):
    """
    注册本代设备中继临时公钥请求
    """

    generation: int = Field(
        ..., ge=1, le=MAX_ACTIVE_DEVICE_SESSION_GENERATION, description="连接材料所属代次"
    )
    spki_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="本端临时证书 SPKI 的 SHA-256 摘要",
    )


class DeviceRelayMaterialData(BaseModel):
    """
    设备中继临时连接材料
    """

    status: Literal["waiting", "ready"] = Field(..., description="对端材料是否已经就绪")
    role: Literal["device", "proxy"] = Field(..., description="当前连接角色")
    generation: int = Field(
        ..., ge=1, le=MAX_ACTIVE_DEVICE_SESSION_GENERATION, description="连接材料所属代次"
    )
    relay_path: str | None = Field(default=None, description="固定控制面密文中继路径")
    relay_ticket: str | None = Field(default=None, description="仅返回一次的短期中继票据")
    peer_spki_sha256: str | None = Field(default=None, description="对端临时证书 SPKI 摘要")
    exporter_context: str | None = Field(default=None, description="TLS exporter 上下文随机值")
    expires_at: datetime | None = Field(default=None, description="本代连接材料到期时间")


class DeviceRelayMaterialResponse(BaseModel):
    """
    设备中继临时连接材料响应
    """

    data: DeviceRelayMaterialData = Field(..., description="设备中继临时连接材料")
    request_id: str | None = Field(default=None, description="请求 ID")


class DeviceControlPolicyData(BaseModel):
    """
    设备控制部署策略数据
    """

    enabled: bool = Field(..., description="部署是否已启用设备控制")
    platform: Literal["macos"] = Field(..., description="当前允许的本机平台")
    protocol_version: int = Field(..., ge=1, description="当前设备控制协议版本")
    lease_seconds: int = Field(..., ge=1, description="设备控制短租约秒数")
    maximum_ttl_seconds: int = Field(..., ge=1, description="设备控制会话最长生命周期秒数")
    relay_maximum_frame_bytes: int = Field(..., ge=1, description="中继单个密文帧最大字节数")
    relay_maximum_bytes_per_second: int = Field(
        ..., ge=1, description="中继每个方向每秒允许的最大密文字节数"
    )
    relay_maximum_connection_seconds: int = Field(
        ..., ge=1, description="中继配对后单次连接最长秒数"
    )
    local_approval_required: Literal[True] = Field(..., description="是否强制要求本机用户审批")


class DeviceControlPolicyResponse(BaseModel):
    """
    设备控制部署策略响应
    """

    data: DeviceControlPolicyData = Field(..., description="设备控制部署策略")
    request_id: str | None = Field(default=None, description="请求 ID")
