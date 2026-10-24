"""
定义固定绑定尝试的结构化 SSH 授权契约。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BindingAttachRequest(BaseModel):
    """
    精确绑定尝试授权请求。
    """

    model_config = ConfigDict(extra="forbid")
    expected_binding_session_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_:-]+$",
        description="已确认的完整绑定尝试标识",
    )


class BindingAttachData(BaseModel):
    """
    不含可执行命令文本的 SSH 连接及任务身份。
    """

    version: Literal[1] = Field(default=1, description="连接契约版本")
    tool_account_id: UUID = Field(..., description="原工具账户标识")
    binding_session_id: str = Field(..., description="原绑定尝试标识")
    node_id: UUID = Field(..., description="绑定节点标识")
    device_id: UUID = Field(..., description="已认证本机设备标识")
    ssh_host: str = Field(..., description="节点 SSH 地址")
    ssh_port: int = Field(..., ge=1, le=65535, description="节点 SSH 端口")
    ssh_user: str = Field(..., description="节点 SSH 网关用户")
    tmux_session_name: str = Field(..., description="绑定终端名称")
    authorization_task_id: str = Field(..., description="原 SSH 密钥同步任务标识")
    authorization_task_status: str = Field(..., description="原 SSH 密钥同步任务状态")
    expires_in: Literal[300] = Field(default=300, description="建议连接窗口秒数而非承载凭据")


class BindingAttachResponse(BaseModel):
    """
    精确绑定连接响应信封。
    """

    data: BindingAttachData = Field(..., description="连接授权和原任务观察")
    request_id: str | None = Field(default=None, description="请求追踪标识")
