from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.schemas.auth import AuthTokenData


class DeviceData(BaseModel):
    """
    设备响应数据
    """

    id: UUID = Field(..., description="设备 ID")
    user_id: UUID = Field(..., description="用户 ID")
    name: str = Field(..., description="设备名称")
    platform: str = Field(..., description="设备平台")
    status: str = Field(..., description="设备状态")
    last_seen_at: datetime | None = Field(default=None, description="最后在线时间")
    created_at: datetime = Field(..., description="创建时间")


class RegisterDeviceRequest(BaseModel):
    """
    注册设备请求
    """

    name: str = Field(..., description="设备名称")
    platform: str = Field(..., description="设备平台")
    ssh_public_key: str = Field(..., description="SSH 公钥")
    wireguard_public_key: str | None = Field(default=None, description="WireGuard 公钥")


class DeviceRegistrationData(BaseModel):
    """
    设备注册响应数据
    """

    device: DeviceData = Field(..., description="设备数据")
    device_token: AuthTokenData = Field(..., description="设备令牌")
    ssh_key_id: UUID = Field(..., description="SSH 公钥 ID")
    wireguard_peer_id: UUID | None = Field(default=None, description="WireGuard peer 标识")


class DeviceRegistrationResponse(BaseModel):
    """
    设备注册响应
    """

    data: DeviceRegistrationData = Field(..., description="设备注册数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class DeviceResponse(BaseModel):
    """
    设备响应
    """

    data: DeviceData = Field(..., description="设备数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class DeviceListData(BaseModel):
    """
    设备列表数据
    """

    items: list[DeviceData] = Field(default_factory=list, description="设备列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class DeviceListResponse(BaseModel):
    """
    设备列表响应
    """

    data: DeviceListData = Field(..., description="设备列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class RotateDeviceTokenResponse(BaseModel):
    """
    设备令牌轮换响应
    """

    data: AuthTokenData = Field(..., description="设备令牌")
    request_id: str | None = Field(default=None, description="请求 ID")
