"""
定义固定 Claude 会话和设备的严格连接与节点验证契约。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExactSessionAttachRequest(BaseModel):
    """
    指定本机已经确认的设备身份。
    """

    model_config = ConfigDict(extra="forbid")
    expected_device_id: UUID = Field(..., description="已确认本机设备标识")


class ExactSessionAttachData(BaseModel):
    """
    返回原授权任务身份及结构化连接字段。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = Field(default=1, description="精确连接协议版本")
    tool_type: Literal["claude"] = Field(default="claude", description="允许挂接的工具类型")
    session_id: UUID = Field(..., description="原工具会话标识")
    device_id: UUID = Field(..., description="原本机设备标识")
    node_id: UUID = Field(..., description="分配节点标识")
    ssh_host: str = Field(..., min_length=1, strict=True, description="节点 SSH 地址")
    ssh_port: int = Field(..., ge=1, le=65535, strict=True, description="节点 SSH 端口")
    ssh_user: str = Field(..., min_length=1, strict=True, description="节点网关用户名")
    tmux_session_name: str = Field(..., min_length=1, strict=True, description="原终端名称")
    forward_ssh_agent: bool = Field(..., strict=True, description="当前策略是否允许转发代理")
    authorization_task_id: str = Field(..., description="原授权逻辑任务标识")
    authorization_task_record_id: UUID = Field(..., description="原授权任务数据库行标识")
    authorization_task_status: Literal[
        "pending", "leased", "running", "succeeded", "failed", "cancelled", "expired"
    ] = Field(..., description="原授权任务明确状态")
    expires_in: Literal[300] = Field(default=300, description="建议连接窗口秒数而非承载凭据")


class ExactSessionAttachResponse(BaseModel):
    """
    包装精确连接授权及原任务观察。
    """

    model_config = ConfigDict(extra="forbid")
    data: ExactSessionAttachData = Field(..., description="连接授权观察")
    request_id: str | None = Field(default=None, description="请求追踪标识")


class VerifyExactSessionAttachRequest(BaseModel):
    """
    仅接受节点强制命令绑定的四个身份。
    """

    model_config = ConfigDict(extra="forbid")
    node_id: UUID = Field(..., description="实际节点标识")
    session_id: UUID = Field(..., description="原会话标识")
    device_id: UUID = Field(..., description="强制命令绑定的设备标识")
    ssh_key_id: UUID = Field(..., description="实际认证密钥标识")


class VerifyExactSessionAttachData(BaseModel):
    """
    回显四个授权身份和最小运行时挂接投影。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = Field(default=1, description="精确验证协议版本")
    tool_type: Literal["claude"] = Field(default="claude", description="获准工具类型")
    node_id: UUID = Field(..., description="已验证节点标识")
    session_id: UUID = Field(..., description="已验证会话标识")
    device_id: UUID = Field(..., description="已验证设备标识")
    ssh_key_id: UUID = Field(..., description="已验证实际密钥标识")
    tmux_session_name: str = Field(..., min_length=1, strict=True, description="原终端名称")
    container_id: str | None = Field(..., description="原容器标识或空值")
    runtime_backend: Literal["native", "docker_sandbox"] = Field(..., description="运行时后端")
    runtime_resource_id: str | None = Field(..., description="原运行时资源标识或空值")
    forward_ssh_agent: bool = Field(..., strict=True, description="当前策略是否允许转发代理")


class VerifyExactSessionAttachResponse(BaseModel):
    """
    包装节点精确挂接验证结果。
    """

    model_config = ConfigDict(extra="forbid")
    data: VerifyExactSessionAttachData = Field(..., description="已验证运行时投影")
    request_id: str | None = Field(default=None, description="请求追踪标识")
