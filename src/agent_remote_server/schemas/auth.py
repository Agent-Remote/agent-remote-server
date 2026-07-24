from pydantic import BaseModel, Field


class BootstrapAdminRequest(BaseModel):
    """
    管理员初始化请求
    """

    username: str = Field(..., description="管理员用户名")
    password: str = Field(..., description="管理员初始密码")
    display_name: str | None = Field(default=None, description="管理员显示名")


class BootstrapStatusData(BaseModel):
    """
    系统初始化状态
    """

    required: bool = Field(..., description="是否需要创建首个管理员")


class BootstrapStatusResponse(BaseModel):
    """
    系统初始化状态响应
    """

    data: BootstrapStatusData = Field(..., description="初始化状态")
    request_id: str | None = Field(default=None, description="请求 ID")


class LoginRequest(BaseModel):
    """
    登录请求
    """

    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")
    totp_code: str | None = Field(default=None, description="TOTP 验证码")


class AuthTokenData(BaseModel):
    """
    认证令牌响应数据
    """

    access_token: str = Field(..., description="访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(..., description="有效秒数")


class AuthTokenResponse(BaseModel):
    """
    认证令牌响应
    """

    data: AuthTokenData = Field(..., description="令牌数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class EmptyResponse(BaseModel):
    """
    空响应
    """

    data: dict[str, object] = Field(default_factory=dict, description="空数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class CliLoginStartData(BaseModel):
    """
    CLI 登录启动数据
    """

    device_code: str = Field(..., description="CLI 设备码")
    user_code: str = Field(..., description="用户确认码")
    verification_url: str = Field(..., description="确认地址")
    expires_in: int = Field(..., description="有效秒数")
    interval: int = Field(..., description="轮询间隔秒数")


class CliLoginStartResponse(BaseModel):
    """
    CLI 登录启动响应
    """

    data: CliLoginStartData = Field(..., description="CLI 登录启动数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class CliLoginApproveRequest(BaseModel):
    """
    CLI 登录确认请求
    """

    user_code: str = Field(..., description="用户确认码")


class CliLoginCompleteRequest(BaseModel):
    """
    CLI 登录完成请求
    """

    device_code: str = Field(..., description="CLI 设备码")


class TotpSetupData(BaseModel):
    """
    TOTP 设置数据
    """

    secret: str = Field(..., description="TOTP 密钥")
    otp_auth_url: str = Field(..., description="Authenticator 导入地址")


class TotpSetupResponse(BaseModel):
    """
    TOTP 设置响应
    """

    data: TotpSetupData = Field(..., description="TOTP 设置数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class TotpVerifyRequest(BaseModel):
    """
    TOTP 验证请求
    """

    code: str = Field(..., description="TOTP 验证码")
