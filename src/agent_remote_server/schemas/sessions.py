from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SessionData(BaseModel):
    """
    工具运行 session 响应数据
    """

    id: UUID = Field(..., description="会话标识")
    tool_type: str = Field(..., description="工具类型")
    user_id: UUID = Field(..., description="用户 ID")
    tool_account_id: UUID = Field(..., description="工具账户 ID")
    workspace_id: UUID = Field(..., description="工作区标识")
    node_id: UUID = Field(..., description="节点 ID")
    project_key: str = Field(..., description="项目 key")
    status: str = Field(..., description="session 状态")
    tmux_session_name: str | None = Field(default=None, description="tmux session 名称")
    container_id: str | None = Field(default=None, description="容器或 sandbox 标识")
    runtime_backend: str = Field(..., description="会话运行时")
    runtime_resource_id: str | None = Field(default=None, description="运行时资源标识")
    replaces_session_id: UUID | None = Field(default=None, description="被替代的会话标识")
    create_task_id: str | None = Field(default=None, description="创建任务 ID")
    stop_task_id: str | None = Field(default=None, description="停止任务 ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateSessionRequest(BaseModel):
    """
    创建工具运行 session 请求
    """

    tool_type: str = Field(..., description="工具类型")
    tool_account_id: UUID = Field(..., description="工具账户 ID")
    workspace_id: UUID = Field(..., description="工作区标识")
    project_key: str = Field(..., description="项目 key")
    argv: list[str] = Field(default_factory=list, description="透传给工具 CLI 的参数")
    replaces_session_id: UUID | None = Field(default=None, description="被替代的中断会话标识")


class SessionResponse(BaseModel):
    """
    工具运行 session 响应
    """

    data: SessionData = Field(..., description="session 数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class SessionListData(BaseModel):
    """
    工具运行 session 列表数据
    """

    items: list[SessionData] = Field(default_factory=list, description="session 列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class SessionListResponse(BaseModel):
    """
    工具运行 session 列表响应
    """

    data: SessionListData = Field(..., description="session 列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")
