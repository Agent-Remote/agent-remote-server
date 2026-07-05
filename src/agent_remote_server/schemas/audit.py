from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AuditLogData(BaseModel):
    """
    审计日志响应数据
    """

    id: UUID = Field(..., description="审计日志 ID")
    actor_user_id: UUID | None = Field(default=None, description="操作者用户 ID")
    action: str = Field(..., description="动作")
    target_type: str | None = Field(default=None, description="目标类型")
    target_id: str | None = Field(default=None, description="目标 ID")
    details: dict[str, object] = Field(default_factory=dict, description="详情")
    created_at: datetime = Field(..., description="创建时间")


class AuditLogResponse(BaseModel):
    """
    审计日志响应
    """

    data: AuditLogData = Field(..., description="审计日志数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class AuditLogListData(BaseModel):
    """
    审计日志列表数据
    """

    items: list[AuditLogData] = Field(default_factory=list, description="审计日志列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class AuditLogListResponse(BaseModel):
    """
    审计日志列表响应
    """

    data: AuditLogListData = Field(..., description="审计日志列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")
