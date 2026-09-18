"""
定义节点接口模型。
"""

from datetime import datetime, timedelta
from typing import Literal
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
    allowed_runtime_backends: list[str] = Field(
        default_factory=lambda: ["docker_sandbox"], description="管理员允许的运行时"
    )
    default_runtime_backend: str = Field(default="docker_sandbox", description="默认运行时")
    runtime_policy: dict[str, object] = Field(default_factory=dict, description="运行时策略")
    runtime_capabilities: dict[str, object] = Field(
        default_factory=dict, description="节点最近上报的运行时能力"
    )
    ego_browser_enabled: bool = Field(
        default=False, description="管理员配置的 ego-browser 能力意图"
    )
    configured_enabled: bool = Field(default=False, description="管理员配置的 ego-browser 能力意图")
    effective_enabled: bool = Field(default=False, description="节点本地制品与策略校验后的有效能力")
    node_execution_allowed: bool = Field(
        default=False, description="节点是否收到 Server execution admission"
    )
    enrollment_admission: bool = Field(
        default=True, description="Server 是否允许节点登记与状态上报"
    )
    execution_admission: bool = Field(default=False, description="Server 是否允许 ego-browser 执行")
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
    allowed_runtime_backends: list[str] = Field(
        default_factory=lambda: ["docker_sandbox"], description="管理员允许的运行时"
    )
    default_runtime_backend: str = Field(default="docker_sandbox", description="默认运行时")
    runtime_policy: dict[str, object] = Field(default_factory=dict, description="运行时策略")
    ego_browser_enabled: bool = Field(
        default=False, description="是否在加入时启用 ego-browser 能力"
    )
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
    allowed_runtime_backends: list[str] | None = Field(default=None, description="允许的运行时")
    default_runtime_backend: str | None = Field(default=None, description="默认运行时")
    runtime_policy: dict[str, object] | None = Field(default=None, description="运行时策略")
    ego_browser_enabled: bool | None = Field(default=None, description="更新 ego-browser 能力意图")
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
    runtime_capabilities: dict[str, object] = Field(
        default_factory=dict, description="运行时能力探测"
    )


class NodeHeartbeatRequest(BaseModel):
    """
    节点心跳请求
    """

    node_id: UUID = Field(..., description="节点 ID")
    version: str = Field(..., description="节点版本")
    supported_tool_types: list[str] = Field(default_factory=list, description="支持工具类型")
    wireguard_ip: str | None = Field(default=None, description="WireGuard 地址")
    wireguard_public_key: str | None = Field(default=None, description="WireGuard 公钥")
    wireguard_endpoint: str | None = Field(default=None, description="WireGuard 连接端点")
    resources: NodeResourcesData = Field(..., description="资源快照")
    runtime: NodeRuntimeData = Field(..., description="运行时快照")


class NodeHeartbeatAdmissionData(BaseModel):
    """
    节点心跳响应中的 enrollment 与执行准入状态。
    """

    enrollment_enabled: bool = Field(..., description="是否允许节点登记与状态上报")
    execution_admission: bool = Field(..., description="是否允许节点广告并执行 ego-browser 能力")


class NodeHeartbeatResponse(BaseModel):
    """
    节点心跳确认及可选准入状态。
    """

    data: NodeHeartbeatAdmissionData = Field(..., description="节点准入状态")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeJoinCodeIssueRequest(BaseModel):
    """
    管理员签发 Node 一次性加入码请求。
    """

    expires_in_seconds: int = Field(default=900, ge=60, le=1800, description="加入码有效秒数")
    ego_browser_enabled: bool | None = Field(
        default=None, description="本次加入是否明确启用 ego-browser 能力"
    )
    exchange_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="受管安装在签发前持久化的幂等交换 ID",
    )


class NodeJoinCodeIssueData(BaseModel):
    """
    Node 加入码首次签发响应。
    """

    node_id: UUID = Field(..., description="目标节点 ID")
    code: str = Field(..., description="仅显示一次的短期加入码")
    expires_at: datetime = Field(..., description="加入码服务端过期时间")
    ego_browser_enabled: bool | None = Field(default=None, description="本次加入的管理员启用意图")


class NodeJoinCodeIssueResponse(BaseModel):
    """
    Node 加入码签发响应。
    """

    data: NodeJoinCodeIssueData = Field(..., description="加入码数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeJoinCodeRevokeRequest(BaseModel):
    """
    管理员撤销 Node 一次性加入码请求。
    """

    exchange_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="只撤销该幂等交换对应的未消费加入码；省略时撤销全部",
    )


class NodeJoinCodeRevokeData(BaseModel):
    """
    Node 一次性加入码撤销结果。
    """

    state: Literal["revoked", "consumed", "missing"] = Field(
        ..., description="精确交换在撤销后的确定状态"
    )


class NodeJoinCodeRevokeResponse(BaseModel):
    """
    Node 一次性加入码撤销响应。
    """

    data: NodeJoinCodeRevokeData = Field(..., description="加入码撤销结果")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeJoinCodeExchangeRequest(BaseModel):
    """
    Node 使用 stdin 加入码交换长期节点凭据请求。
    """

    node_id: UUID | None = Field(default=None, description="目标节点 ID，可由加入码推断")
    version: str = Field(..., min_length=1, max_length=64, description="节点版本")
    join_code: str | None = Field(
        default=None,
        min_length=16,
        max_length=4096,
        description="短期加入码；恢复已消费交换时可省略",
    )
    exchange_id: str = Field(..., min_length=16, max_length=128, description="本次交换 ID")
    release_profile: str | None = Field(
        default=None, max_length=128, description="期望的加入码发布 profile"
    )
    wrapper_version: str | None = Field(
        default=None, max_length=64, description="期望的 wrapper 版本"
    )
    skill_version: str | None = Field(default=None, max_length=64, description="期望的 Skill 版本")
    runtime_version: str | None = Field(
        default=None, max_length=64, description="期望的 Node runtime 版本"
    )
    artifact_digest: str | None = Field(default=None, max_length=128, description="期望的制品摘要")
    profile_digest: str | None = Field(
        default=None, max_length=128, description="期望的 profile 摘要"
    )
    ego_browser_enabled: bool | None = Field(
        default=None, description="期望的 ego-browser 启用意图"
    )


class NodeJoinCodeExchangeData(BaseModel):
    """
    Node 加入码交换结果。
    """

    node_id: UUID = Field(..., description="已加入的节点 ID")
    node_token: str = Field(..., description="仅交换响应返回的节点凭据")
    ego_browser_enabled: bool = Field(..., description="节点配置的 ego-browser 意图")
    exchange_id: str = Field(..., description="原样返回的交换 ID")
    server_origin: str = Field(..., description="加入码绑定的 Server origin")
    release_profile: str = Field(..., description="加入码绑定的发布 profile")
    wrapper_version: str = Field(..., description="加入码绑定的 wrapper 版本")
    skill_version: str = Field(..., description="加入码绑定的 Skill 版本")
    runtime_version: str | None = Field(default=None, description="加入码绑定的 Node runtime 版本")
    artifact_digest: str = Field(..., description="加入码绑定的制品摘要")
    profile_digest: str = Field(..., description="加入码绑定的 profile 摘要")
    ego_browser_enabled_intent: bool | None = Field(
        default=None, description="加入码携带的原始 ego-browser 启用意图"
    )


class NodeJoinCodeExchangeResponse(BaseModel):
    """
    Node 加入码交换响应。
    """

    data: NodeJoinCodeExchangeData = Field(..., description="交换结果")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeWireGuardPeerData(BaseModel):
    """
    节点侧 WireGuard 对等端数据
    """

    public_key: str = Field(..., description="设备 WireGuard 公钥")
    allowed_ips: list[str] = Field(..., description="设备允许地址")


class NodeWireGuardPeerListData(BaseModel):
    """
    节点侧 WireGuard 对等端列表数据
    """

    items: list[NodeWireGuardPeerData] = Field(default_factory=list, description="对等端列表")


class NodeWireGuardPeerListResponse(BaseModel):
    """
    节点侧 WireGuard 对等端列表响应
    """

    data: NodeWireGuardPeerListData = Field(..., description="对等端列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


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


class NodeTaskResultData(BaseModel):
    """
    节点任务结果响应数据
    """

    status: str = Field(..., description="结果状态")
    result: dict[str, object] | None = Field(default=None, description="结果数据")
    error: dict[str, object] | None = Field(default=None, description="错误信息")
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="完成时间")
    created_at: datetime = Field(..., description="创建时间")


class NodeTaskData(BaseModel):
    """
    节点任务响应数据
    """

    id: UUID = Field(..., description="节点任务 ID")
    task_id: str = Field(..., description="任务 ID")
    node_id: UUID = Field(..., description="节点 ID")
    task_type: str = Field(..., description="任务类型")
    status: str = Field(..., description="任务状态")
    payload: dict[str, object] = Field(default_factory=dict, description="任务载荷")
    lease_until: datetime | None = Field(default=None, description="租约过期时间")
    retry_count: int = Field(..., description="重试次数")
    result: NodeTaskResultData | None = Field(default=None, description="任务结果")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class NodeTaskResponse(BaseModel):
    """
    节点任务响应
    """

    data: NodeTaskData = Field(..., description="节点任务数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class NodeTaskListData(BaseModel):
    """
    节点任务列表数据
    """

    items: list[NodeTaskData] = Field(default_factory=list, description="节点任务列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class NodeTaskListResponse(BaseModel):
    """
    节点任务列表响应
    """

    data: NodeTaskListData = Field(..., description="节点任务列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


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
