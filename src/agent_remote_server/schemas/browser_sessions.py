from datetime import datetime
from uuid import UUID

from pydantic import AnyUrl, BaseModel, Field


class BrowserSessionData(BaseModel):
    """
    远端临时浏览器 session 响应数据
    """

    id: UUID = Field(..., description="浏览器 session 标识")
    user_id: UUID = Field(..., description="用户 ID")
    tool_account_id: UUID | None = Field(default=None, description="工具账户 ID")
    node_id: UUID = Field(..., description="节点 ID")
    status: str = Field(..., description="浏览器 session 状态")
    region_code: str = Field(..., description="地区代码")
    timezone: str = Field(..., description="时区")
    locale: str = Field(..., description="区域语言")
    target_url: str | None = Field(default=None, description="初始 URL")
    container_id: str | None = Field(default=None, description="浏览器容器标识")
    ttl_seconds: int = Field(..., description="会话 TTL 秒数")
    expires_at: datetime = Field(..., description="过期时间")
    stopped_at: datetime | None = Field(default=None, description="停止时间")
    create_task_id: str | None = Field(default=None, description="创建任务 ID")
    stop_task_id: str | None = Field(default=None, description="停止任务 ID")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateBrowserSessionRequest(BaseModel):
    """
    创建远端临时浏览器 session 请求
    """

    tool_account_id: UUID | None = Field(default=None, description="工具账户 ID")
    target_url: AnyUrl | None = Field(default=None, description="初始 URL")
    region_code: str | None = Field(default=None, description="地区代码")
    timezone: str | None = Field(default=None, description="时区")
    locale: str | None = Field(default=None, description="区域语言")
    ttl_seconds: int = Field(default=1800, ge=60, le=7200, description="会话 TTL 秒数")


class BrowserSessionResponse(BaseModel):
    """
    远端临时浏览器 session 响应
    """

    data: BrowserSessionData = Field(..., description="浏览器 session 数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class BrowserSessionListData(BaseModel):
    """
    远端临时浏览器 session 列表数据
    """

    items: list[BrowserSessionData] = Field(default_factory=list, description="session 列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class BrowserSessionListResponse(BaseModel):
    """
    远端临时浏览器 session 列表响应
    """

    data: BrowserSessionListData = Field(..., description="session 列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class BrowserConnectInfoData(BaseModel):
    """
    浏览器内嵌连接信息
    """

    browser_session_id: UUID = Field(..., description="浏览器 session 标识")
    status: str = Field(..., description="浏览器 session 状态")
    embed_url: str = Field(..., description="短期内嵌 URL")
    expires_at: datetime = Field(..., description="连接信息过期时间")


class BrowserConnectInfoResponse(BaseModel):
    """
    浏览器内嵌连接信息响应
    """

    data: BrowserConnectInfoData = Field(..., description="内嵌连接信息")
    request_id: str | None = Field(default=None, description="请求 ID")
