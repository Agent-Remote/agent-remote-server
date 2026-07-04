from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, Field


class NodeData(BaseModel):
    """
    节点响应数据
    """

    id: UUID = Field(..., description="节点 ID")
    name: str = Field(..., description="节点名称")
    status: str = Field(..., description="节点状态")
    region_code: str = Field(..., description="地区代码")
    tags: list[str] = Field(default_factory=list, description="节点标签")
    weight: int = Field(..., description="调度权重")
    wireguard_ip: str | None = Field(default=None, description="WireGuard 地址")
    wireguard_public_key: str | None = Field(default=None, description="WireGuard 公钥")
    wireguard_endpoint: str | None = Field(default=None, description="WireGuard 连接端点")
    ssh_host: str | None = Field(default=None, description="SSH 主机")
    ssh_port: int | None = Field(default=None, description="SSH 端口")
    ssh_user: str | None = Field(default=None, description="SSH 用户")
    supported_tool_types: list[str] = Field(default_factory=list, description="支持工具类型")
    last_heartbeat_at: datetime | None = Field(default=None, description="最后心跳时间")
    version: str | None = Field(default=None, description="节点版本")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateNodeRequest(BaseModel):
    """
    创建节点请求
    """

    name: str = Field(..., description="节点名称")
    region_code: str = Field(..., description="地区代码")
    tags: list[str] = Field(default_factory=list, description="节点标签")
    weight: int = Field(default=100, description="调度权重")
    supported_tool_types: list[str] = Field(default_factory=list, description="支持工具类型")
    wireguard_ip: str | None = Field(default=None, description="WireGuard 地址")
    wireguard_public_key: str | None = Field(default=None, description="WireGuard 公钥")
    wireguard_endpoint: str | None = Field(default=None, description="WireGuard 连接端点")
    ssh_host: str | None = Field(default=None, description="SSH 主机")
    ssh_port: int | None = Field(default=None, description="SSH 端口")
    ssh_user: str | None = Field(default=None, description="SSH 用户")


class UpdateNodeRequest(BaseModel):
    """
    更新节点请求
    """

    name: str | None = Field(default=None, description="节点名称")
    status: str | None = Field(default=None, description="节点状态")
    tags: list[str] | None = Field(default=None, description="节点标签")
    weight: int | None = Field(default=None, description="调度权重")
    supported_tool_types: list[str] | None = Field(default=None, description="支持工具类型")
    wireguard_ip: str | None = Field(default=None, description="WireGuard 地址")
    wireguard_public_key: str | None = Field(default=None, description="WireGuard 公钥")
    wireguard_endpoint: str | None = Field(default=None, description="WireGuard 连接端点")
    ssh_host: str | None = Field(default=None, description="SSH 主机")
    ssh_port: int | None = Field(default=None, description="SSH 端口")
    ssh_user: str | None = Field(default=None, description="SSH 用户")


class NodeRegistrationTokenData(BaseModel):
    """
    节点注册 token 数据
    """

    node: NodeData = Field(..., description="节点数据")
    registration_token: str = Field(..., description="节点注册 token")


class NodeRegistrationTokenResponse(BaseModel):
    """
    节点注册 token 响应
    """

    data: NodeRegistrationTokenData = Field(..., description="节点注册 token 数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeResponse(BaseModel):
    """
    节点响应
    """

    data: NodeData = Field(..., description="节点数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeListData(BaseModel):
    """
    节点列表数据
    """

    items: list[NodeData] = Field(default_factory=list, description="节点列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class NodeListResponse(BaseModel):
    """
    节点列表响应
    """

    data: NodeListData = Field(..., description="节点列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeRegisterRequest(BaseModel):
    """
    节点注册请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    registration_token: str = Field(..., description="节点注册 token")
    version: str = Field(..., description="节点版本")


class NodeRegisterData(BaseModel):
    """
    节点注册响应数据
    """

    node_id: UUID = Field(..., description="节点 ID")
    node_token: str = Field(..., description="节点 token")


class NodeRegisterResponse(BaseModel):
    """
    节点注册响应
    """

    data: NodeRegisterData = Field(..., description="节点注册响应数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeResourcesData(BaseModel):
    """
    节点资源快照
    """

    cpu_load: float = Field(..., description="CPU 负载")
    memory_used_bytes: int = Field(..., description="已用内存字节数")
    memory_total_bytes: int = Field(..., description="总内存字节数")
    disk_used_bytes: int = Field(..., description="已用磁盘字节数")
    disk_total_bytes: int = Field(..., description="总磁盘字节数")


class NodeRuntimeData(BaseModel):
    """
    节点运行时快照
    """

    docker_ok: bool = Field(..., description="Docker 是否可用")
    tmux_ok: bool = Field(..., description="Tmux 是否可用")
    active_sessions: int = Field(default=0, description="活跃会话数量")
    active_browser_sessions: int = Field(default=0, description="活跃浏览器会话数量")
    containers: int = Field(default=0, description="容器数量")


class NodeHeartbeatRequest(BaseModel):
    """
    节点心跳请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    version: str = Field(..., description="节点版本")
    supported_tool_types: list[str] = Field(default_factory=list, description="支持工具类型")
    resources: NodeResourcesData = Field(..., description="资源快照")
    runtime: NodeRuntimeData = Field(..., description="运行时快照")


class NodeTaskEnvelope(BaseModel):
    """
    节点任务信封
    """

    task_id: str = Field(..., description="任务 ID")
    node_id: UUID = Field(..., description="节点 ID")
    task_type: str = Field(..., description="任务类型")
    idempotency_key: str = Field(..., description="幂等键")
    payload: dict[str, object] = Field(default_factory=dict, description="任务载荷")
    lease_until: datetime = Field(..., description="租约过期时间")
    created_at: datetime = Field(..., description="创建时间")
    expires_at: datetime = Field(..., description="过期时间")


class NodeTaskPollData(BaseModel):
    """
    节点任务轮询数据
    """

    tasks: list[NodeTaskEnvelope] = Field(default_factory=list, description="任务列表")


class NodeTaskPollResponse(BaseModel):
    """
    节点任务轮询响应
    """

    data: NodeTaskPollData = Field(..., description="任务轮询数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class CompleteNodeTaskRequest(BaseModel):
    """
    完成节点任务请求
    """

    result: dict[str, object] = Field(default_factory=dict, description="任务结果")


class FailNodeTaskRequest(BaseModel):
    """
    失败节点任务请求
    """

    error: dict[str, object] = Field(default_factory=dict, description="错误信息")


class ReconcileRequest(BaseModel):
    """
    节点对账请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    sections: list[str] = Field(default_factory=list, description="对账分区")
    snapshot: dict[str, object] = Field(default_factory=dict, description="对账快照")


def task_expires_at(created_at: datetime) -> datetime:
    """
    计算任务默认过期时间

    :param created_at (datetime): 创建时间

    :return datetime: 过期时间
    """

    return created_at + timedelta(days=1)
