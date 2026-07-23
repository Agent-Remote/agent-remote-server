from uuid import UUID

from pydantic import BaseModel, Field


class WireGuardNodePeerData(BaseModel):
    """
    节点 WireGuard peer 数据
    """

    node_id: UUID = Field(..., description="节点 ID")
    name: str = Field(..., description="节点名称")
    region_code: str = Field(..., description="地区代码")
    public_key: str = Field(..., description="节点 WireGuard 公钥")
    endpoint: str = Field(..., description="节点 WireGuard endpoint")
    allowed_ips: list[str] = Field(default_factory=list, description="允许路由")
    persistent_keepalive_seconds: int = Field(default=25, description="keepalive 秒数")


class WireGuardConfigData(BaseModel):
    """
    设备 WireGuard 配置数据
    """

    device_id: UUID = Field(..., description="设备 ID")
    interface_address: str = Field(..., description="设备 WireGuard 地址")
    private_key_ref: str = Field(..., description="本地私钥引用")
    dns: list[str] = Field(default_factory=list, description="DNS 服务器")
    peers: list[WireGuardNodePeerData] = Field(default_factory=list, description="节点 peers")


class WireGuardConfigResponse(BaseModel):
    """
    WireGuard 配置响应
    """

    data: WireGuardConfigData = Field(..., description="WireGuard 配置")
    request_id: str | None = Field(default=None, description="请求 ID")


class AttachSessionData(BaseModel):
    """
    SSH attach 授权数据
    """

    session_id: UUID = Field(..., description="会话 ID")
    node_id: UUID = Field(..., description="节点 ID")
    node_wireguard_ip: str = Field(..., description="节点 WireGuard IP")
    ssh_host: str = Field(..., description="SSH 主机")
    ssh_port: int = Field(..., description="SSH 端口")
    ssh_user: str = Field(..., description="SSH 用户")
    tmux_session_name: str = Field(..., description="tmux session 名称")
    command_args: list[str] = Field(default_factory=list, description="远端命令参数")
    ssh_command: str = Field(..., description="推荐 SSH 命令")
    authorization_task_id: str = Field(..., description="SSH key 同步任务 ID")
    expires_in: int = Field(..., description="授权建议缓存秒数")


class AttachSessionResponse(BaseModel):
    """
    SSH attach 授权响应
    """

    data: AttachSessionData = Field(..., description="SSH attach 授权数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class VerifyAttachRequest(BaseModel):
    """
    节点 attach 校验请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    session_id: UUID = Field(..., description="会话 ID")
    device_id: UUID = Field(..., description="设备 ID")


class VerifyAttachData(BaseModel):
    """
    节点 attach 校验数据
    """

    session_id: UUID = Field(..., description="会话 ID")
    tmux_session_name: str = Field(..., description="tmux session 名称")
    container_id: str | None = Field(default=None, description="容器 ID")
    runtime_backend: str = Field(..., description="会话运行时")
    runtime_resource_id: str | None = Field(default=None, description="运行时资源标识")


class VerifyAttachResponse(BaseModel):
    """
    节点 attach 校验响应
    """

    data: VerifyAttachData = Field(..., description="节点 attach 校验数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class VerifyBindingAttachRequest(BaseModel):
    """
    节点绑定会话 attach 校验请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    tool_account_id: UUID = Field(..., description="工具账户 ID")
    device_id: UUID = Field(..., description="设备 ID")


class VerifyBindingAttachData(BaseModel):
    """
    节点绑定会话 attach 校验数据
    """

    binding_session_id: str = Field(..., description="绑定运行时会话 ID")
    tmux_session_name: str = Field(..., description="tmux session 名称")
    runtime_backend: str = Field(..., description="账户运行时")


class VerifyBindingAttachResponse(BaseModel):
    """
    节点绑定会话 attach 校验响应
    """

    data: VerifyBindingAttachData = Field(..., description="绑定 attach 校验数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class VerifySyncRequest(BaseModel):
    """
    节点同步 gateway 校验请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    device_id: UUID = Field(..., description="设备 ID")


class VerifySyncData(BaseModel):
    """
    节点同步 gateway 校验数据
    """

    user_id: UUID = Field(..., description="同步运行用户 ID")


class VerifySyncResponse(BaseModel):
    """
    节点同步 gateway 校验响应
    """

    data: VerifySyncData = Field(..., description="同步 gateway 校验数据")
    request_id: str | None = Field(default=None, description="请求 ID")
