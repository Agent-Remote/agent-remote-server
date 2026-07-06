from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class GitIdentity(BaseModel):
    """
    Git 身份配置
    """

    user_name: str | None = Field(default=None, description="Git 用户名")
    user_email: str | None = Field(default=None, description="Git 邮箱")


class GitHubCliConfig(BaseModel):
    """
    GitHub CLI 配置
    """

    mode: str = Field(default="remote_login", description="gh 认证模式")


class SshCredentialConfig(BaseModel):
    """
    SSH 凭据配置
    """

    mode: str = Field(default="agent_forwarding", description="SSH 注入模式")


class DeveloperCredentialProfileData(BaseModel):
    """
    开发凭据 profile 响应数据
    """

    id: UUID = Field(..., description="开发凭据 profile ID")
    user_id: UUID = Field(..., description="用户 ID")
    display_name: str = Field(..., description="显示名称")
    status: str = Field(..., description="状态")
    git_identity: dict[str, object] = Field(default_factory=dict, description="Git 身份")
    github_cli_mode: str = Field(..., description="gh 认证模式")
    ssh_mode: str = Field(..., description="SSH 注入模式")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateDeveloperCredentialProfileRequest(BaseModel):
    """
    创建开发凭据 profile 请求
    """

    display_name: str = Field(..., description="显示名称")
    git_identity: GitIdentity = Field(default_factory=GitIdentity, description="Git 身份")
    github_cli: GitHubCliConfig = Field(
        default_factory=GitHubCliConfig,
        description="GitHub CLI 配置",
    )
    ssh: SshCredentialConfig = Field(default_factory=SshCredentialConfig, description="SSH 配置")


class UpdateDeveloperCredentialProfileRequest(BaseModel):
    """
    更新开发凭据 profile 请求
    """

    display_name: str | None = Field(default=None, description="显示名称")
    status: str | None = Field(default=None, description="状态")
    git_identity: GitIdentity | None = Field(default=None, description="Git 身份")
    github_cli: GitHubCliConfig | None = Field(default=None, description="GitHub CLI 配置")
    ssh: SshCredentialConfig | None = Field(default=None, description="SSH 配置")


class BindDeveloperCredentialProfileRequest(BaseModel):
    """
    绑定开发凭据 profile 请求
    """

    profile_id: UUID = Field(..., description="开发凭据 profile ID")


class DeveloperCredentialProfileResponse(BaseModel):
    """
    开发凭据 profile 响应
    """

    data: DeveloperCredentialProfileData = Field(..., description="开发凭据 profile 数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class DeveloperCredentialProfileListData(BaseModel):
    """
    开发凭据 profile 列表数据
    """

    items: list[DeveloperCredentialProfileData] = Field(
        default_factory=list,
        description="开发凭据 profile 列表",
    )
    next_cursor: str | None = Field(default=None, description="下一页游标")


class DeveloperCredentialProfileListResponse(BaseModel):
    """
    开发凭据 profile 列表响应
    """

    data: DeveloperCredentialProfileListData = Field(..., description="列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")
