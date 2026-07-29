from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictPortForwardRequest(BaseModel):
    """拒绝未声明字段的端口转发请求基类。"""

    model_config = ConfigDict(extra="forbid")


class CreatePortForwardRequest(StrictPortForwardRequest):
    """
    创建 session 端口转发请求
    """

    remote_port: int = Field(..., ge=1, le=65535, description="Runtime 远端端口")
    local_port: int = Field(..., ge=1, le=65535, description="客户端请求的本地端口")
    client_instance_id: str = Field(..., min_length=1, max_length=128, description="CLI 实例 ID")
    ttl_seconds: int | None = Field(default=None, ge=60, description="请求的绝对有效秒数")


class PortForwardConnectionData(BaseModel):
    """
    一次性端口转发连接凭证
    """

    token: str = Field(..., description="仅返回一次的短期连接 token")
    expires_at: datetime = Field(..., description="连接 token 过期时间")


class PortForwardData(BaseModel):
    """
    Session 端口转发数据
    """

    id: UUID = Field(..., description="端口转发 ID")
    user_id: UUID = Field(..., description="所属用户 ID")
    device_id: UUID = Field(..., description="所属设备 ID")
    session_id: UUID = Field(..., description="工具 session ID")
    node_id: UUID = Field(..., description="节点 ID")
    remote_port: int = Field(..., description="Runtime 远端端口")
    requested_local_port: int = Field(..., description="客户端请求的本地端口")
    client_instance_id: str = Field(..., description="CLI 实例 ID")
    status: str = Field(..., description="端口转发状态")
    bytes_up: int = Field(..., description="累计上行字节数")
    bytes_down: int = Field(..., description="累计下行字节数")
    connection_count: int = Field(..., description="累计 TCP 连接数")
    last_connected_at: datetime | None = Field(default=None, description="最近连接时间")
    lease_expires_at: datetime | None = Field(default=None, description="当前授权租约过期时间")
    expires_at: datetime = Field(..., description="绝对过期时间")
    stopped_at: datetime | None = Field(default=None, description="停止时间")
    stop_reason: str | None = Field(default=None, description="停止原因码")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class PortForwardCreatedData(PortForwardData):
    """
    新建端口转发数据
    """

    node_wireguard_ip: str = Field(..., description="Node WireGuard 地址")
    ssh_user: str = Field(..., description="Node SSH 用户")
    ssh_port: int = Field(..., description="Node SSH 端口")
    connection: PortForwardConnectionData = Field(..., description="一次性连接凭证")


class PortForwardResponse(BaseModel):
    """
    Session 端口转发响应
    """

    data: PortForwardData = Field(..., description="端口转发数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class PortForwardCreatedResponse(BaseModel):
    """
    新建 Session 端口转发响应
    """

    data: PortForwardCreatedData = Field(..., description="新建端口转发数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class PortForwardListData(BaseModel):
    """
    Session 端口转发列表数据
    """

    items: list[PortForwardData] = Field(default_factory=list, description="端口转发列表")


class PortForwardListResponse(BaseModel):
    """
    Session 端口转发列表响应
    """

    data: PortForwardListData = Field(..., description="端口转发列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class PortForwardConnectionResponse(BaseModel):
    """
    端口转发重连凭证响应
    """

    data: PortForwardConnectionData = Field(..., description="一次性连接凭证")
    request_id: str | None = Field(default=None, description="请求 ID")


class RedeemPortForwardRequest(StrictPortForwardRequest):
    """
    Node 兑换端口转发连接凭证请求
    """

    forward_id: UUID = Field(..., description="端口转发 ID")
    device_id: UUID = Field(..., description="SSH forced-command 绑定设备 ID")
    ssh_key_id: UUID = Field(..., description="SSH forced-command 绑定公钥 ID")
    connect_token: str = Field(..., min_length=32, max_length=256, description="一次性连接 token")


class PortForwardLeaseData(BaseModel):
    """
    Node 端口转发授权租约数据
    """

    forward_id: UUID = Field(..., description="端口转发 ID")
    session_id: UUID = Field(..., description="工具 session ID")
    runtime_backend: str = Field(..., description="运行时后端")
    runtime_resource_id: str = Field(..., description="运行时资源 ID")
    remote_port: int = Field(..., description="已授权 runtime loopback 端口")
    generation: int = Field(..., description="连接代次")
    lease_expires_at: datetime = Field(..., description="授权租约过期时间")
    max_streams: int = Field(..., description="最大并发 stream 数")
    bytes_per_second: int = Field(..., description="每方向带宽上限，零表示不限制")
    control_plane_grace_seconds: int = Field(..., description="控制面不可用宽限秒数")


class PortForwardLeaseResponse(BaseModel):
    """
    Node 端口转发授权租约响应
    """

    data: PortForwardLeaseData = Field(..., description="授权租约数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class RenewPortForwardRequest(StrictPortForwardRequest):
    """
    Node 续租端口转发请求
    """

    generation: int = Field(..., ge=1, description="当前连接代次")
    bytes_up_total: int = Field(default=0, ge=0, description="当前代次累计上行字节数")
    bytes_down_total: int = Field(default=0, ge=0, description="当前代次累计下行字节数")
    connection_count_total: int = Field(default=0, ge=0, description="当前代次累计 TCP 连接数")


class ReleasePortForwardRequest(RenewPortForwardRequest):
    """
    Node 释放端口转发请求
    """

    reason: str = Field(default="tunnel_disconnected", max_length=64, description="释放原因码")
