from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class WorkspaceData(BaseModel):
    """
    workspace 响应数据
    """

    id: UUID = Field(..., description="工作区标识")
    user_id: UUID = Field(..., description="用户 ID")
    device_id: UUID = Field(..., description="设备 ID")
    project_key: str = Field(..., description="项目 key")
    local_start_path: str = Field(..., description="本地启动路径")
    display_name: str = Field(..., description="显示名称")
    remote_path: str | None = Field(default=None, description="远端路径")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateWorkspaceRequest(BaseModel):
    """
    创建 workspace 请求
    """

    device_id: UUID = Field(..., description="设备 ID")
    project_key: str = Field(..., description="项目 key")
    local_start_path: str = Field(..., description="本地启动路径")
    display_name: str = Field(..., description="显示名称")


class UpdateWorkspaceRequest(BaseModel):
    """
    更新 workspace 请求
    """

    local_start_path: str | None = Field(default=None, description="本地启动路径")
    display_name: str | None = Field(default=None, description="显示名称")


class WorkspaceResponse(BaseModel):
    """
    workspace 响应
    """

    data: WorkspaceData = Field(..., description="workspace 数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class WorkspaceListData(BaseModel):
    """
    workspace 列表数据
    """

    items: list[WorkspaceData] = Field(default_factory=list, description="workspace 列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class WorkspaceListResponse(BaseModel):
    """
    workspace 列表响应
    """

    data: WorkspaceListData = Field(..., description="workspace 列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class SyncSessionData(BaseModel):
    """
    同步 session 响应数据
    """

    id: UUID = Field(..., description="同步 session ID")
    user_id: UUID = Field(..., description="用户 ID")
    workspace_id: UUID = Field(..., description="工作区标识")
    node_id: UUID | None = Field(default=None, description="节点 ID")
    local_path: str = Field(..., description="本地路径")
    remote_path: str = Field(..., description="远端路径")
    status: str = Field(..., description="同步状态")
    conflict_status: str = Field(..., description="冲突状态")
    sync_mode: str = Field(..., description="同步模式")
    mutagen_session_id: str | None = Field(default=None, description="Mutagen 会话标识")
    remote_endpoint: str | None = Field(default=None, description="Mutagen 远端 endpoint")
    prepare_task_id: str | None = Field(default=None, description="workspace 准备任务 ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateSyncSessionRequest(BaseModel):
    """
    创建同步 session 请求
    """

    workspace_id: UUID = Field(..., description="工作区标识")
    node_id: UUID | None = Field(default=None, description="节点 ID")
    local_path: str | None = Field(default=None, description="本地路径")
    sync_mode: str = Field(default="two_way", description="同步模式")


class SyncSessionActionRequest(BaseModel):
    """
    同步 session 操作请求
    """

    note: str | None = Field(default=None, description="操作备注")


class SyncSessionResponse(BaseModel):
    """
    同步 session 响应
    """

    data: SyncSessionData = Field(..., description="同步 session 数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class SyncSessionListData(BaseModel):
    """
    同步 session 列表数据
    """

    items: list[SyncSessionData] = Field(default_factory=list, description="同步 session 列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class SyncSessionListResponse(BaseModel):
    """
    同步 session 列表响应
    """

    data: SyncSessionListData = Field(..., description="同步 session 列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")
