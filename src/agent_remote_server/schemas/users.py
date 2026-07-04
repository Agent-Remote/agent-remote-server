from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class UserData(BaseModel):
    """
    用户响应数据
    """

    id: UUID = Field(..., description="用户 ID")
    username: str = Field(..., description="用户名")
    display_name: str = Field(..., description="显示名")
    role: str = Field(..., description="角色")
    status: str = Field(..., description="状态")
    totp_enabled: bool = Field(..., description="是否启用 TOTP")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateUserRequest(BaseModel):
    """
    创建用户请求
    """

    username: str = Field(..., description="用户名")
    password: str = Field(..., description="初始密码")
    role: str = Field(..., description="角色")
    display_name: str | None = Field(default=None, description="显示名")


class UpdateUserRequest(BaseModel):
    """
    更新用户请求
    """

    display_name: str | None = Field(default=None, description="显示名")
    status: str | None = Field(default=None, description="状态")


class UpdateMeRequest(BaseModel):
    """
    更新当前用户请求
    """

    display_name: str = Field(..., description="显示名")


class UserResponse(BaseModel):
    """
    用户响应
    """

    data: UserData = Field(..., description="用户数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class UserListData(BaseModel):
    """
    用户列表数据
    """

    items: list[UserData] = Field(default_factory=list, description="用户列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class UserListResponse(BaseModel):
    """
    用户列表响应
    """

    data: UserListData = Field(..., description="用户列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")
