from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ToolAccountData(BaseModel):
    """
    工具账户响应数据
    """

    id: UUID = Field(..., description="工具账户标识")
    user_id: UUID = Field(..., description="用户标识")
    tool_type: str = Field(..., description="工具类型")
    display_name: str = Field(..., description="显示名称")
    status: str = Field(..., description="账户状态")
    region_code: str = Field(..., description="地区代码")
    timezone: str = Field(..., description="时区")
    locale: str = Field(..., description="区域设置")
    preferred_node_tags: list[str] = Field(default_factory=list, description="偏好节点标签")
    affinity_node_id: UUID | None = Field(default=None, description="亲和节点标识")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateToolAccountRequest(BaseModel):
    """
    创建工具账户请求
    """

    tool_type: str = Field(..., description="工具类型")
    display_name: str = Field(..., description="显示名称")
    region_code: str = Field(..., description="地区代码")
    timezone: str = Field(..., description="时区")
    locale: str = Field(..., description="区域设置")
    preferred_node_tags: list[str] = Field(default_factory=list, description="偏好节点标签")


class UpdateToolAccountRequest(BaseModel):
    """
    更新工具账户请求
    """

    display_name: str | None = Field(default=None, description="显示名称")
    status: str | None = Field(default=None, description="账户状态")
    region_code: str | None = Field(default=None, description="地区代码")
    timezone: str | None = Field(default=None, description="时区")
    locale: str | None = Field(default=None, description="区域设置")
    preferred_node_tags: list[str] | None = Field(default=None, description="偏好节点标签")


class ToolAccountResponse(BaseModel):
    """
    工具账户响应
    """

    data: ToolAccountData = Field(..., description="工具账户数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class ToolAccountListData(BaseModel):
    """
    工具账户列表数据
    """

    items: list[ToolAccountData] = Field(default_factory=list, description="工具账户列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class ToolAccountListResponse(BaseModel):
    """
    工具账户列表响应
    """

    data: ToolAccountListData = Field(..., description="工具账户列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class BindingStatusData(BaseModel):
    """
    绑定状态数据
    """

    tool_account_id: UUID = Field(..., description="工具账户标识")
    status: str = Field(..., description="绑定状态")
    node_id: UUID | None = Field(default=None, description="节点标识")
    binding_session_id: str | None = Field(default=None, description="绑定会话标识")
    tmux_session_name: str | None = Field(default=None, description="tmux 会话名称")
    account_remote_path: str | None = Field(default=None, description="账户远端路径")
    connect_command: str | None = Field(default=None, description="连接命令")
    task_id: str | None = Field(default=None, description="任务标识")
    verifier: str | None = Field(default=None, description="校验器名称")
    error: str | None = Field(default=None, description="错误摘要")


class BindingStatusResponse(BaseModel):
    """
    绑定状态响应
    """

    data: BindingStatusData = Field(..., description="绑定状态数据")
    request_id: str | None = Field(default=None, description="请求 ID")
