from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_token,
    get_current_user,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import AuthToken, User
from agent_remote_server.schemas.auth import (
    AuthTokenData,
    AuthTokenResponse,
    BootstrapAdminRequest,
    CliLoginApproveRequest,
    CliLoginCompleteRequest,
    CliLoginStartData,
    CliLoginStartResponse,
    EmptyResponse,
    LoginRequest,
    TotpSetupData,
    TotpSetupResponse,
    TotpVerifyRequest,
)
from agent_remote_server.services.identity import IdentityService, TokenIssue

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_data(token_issue: TokenIssue) -> AuthTokenData:
    return AuthTokenData(
        access_token=token_issue.raw_token,
        token_type="bearer",
        expires_in=token_issue.expires_in,
    )


@router.post("/bootstrap", response_model=AuthTokenResponse)
async def bootstrap_admin(
    payload: BootstrapAdminRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthTokenResponse:
    """
    初始化第一个管理员

    :param payload (BootstrapAdminRequest): 初始化请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话

    :return AuthTokenResponse: 登录令牌响应
    """

    token_issue = await IdentityService(session, settings).bootstrap_admin(
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
    )
    return AuthTokenResponse(data=_token_data(token_issue), request_id=get_request_id())


@router.post("/login", response_model=AuthTokenResponse)
async def login(
    payload: LoginRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthTokenResponse:
    """
    用户名密码登录

    :param payload (LoginRequest): 登录请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话

    :return AuthTokenResponse: 登录令牌响应
    """

    token_issue = await IdentityService(session, settings).login(
        username=payload.username,
        password=payload.password,
        totp_code=payload.totp_code,
    )
    return AuthTokenResponse(data=_token_data(token_issue), request_id=get_request_id())


@router.post("/logout", response_model=EmptyResponse)
async def logout(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> EmptyResponse:
    """
    注销当前令牌

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前令牌

    :return EmptyResponse: 空响应
    """

    await IdentityService(session, settings).logout(token)
    return EmptyResponse(request_id=get_request_id())


@router.post("/refresh", response_model=AuthTokenResponse)
async def refresh(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> AuthTokenResponse:
    """
    刷新当前令牌

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前令牌

    :return AuthTokenResponse: 新令牌响应
    """

    token_issue = await IdentityService(session, settings).refresh_token(token)
    return AuthTokenResponse(data=_token_data(token_issue), request_id=get_request_id())


@router.post("/cli/start", response_model=CliLoginStartResponse)
async def start_cli_login(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CliLoginStartResponse:
    """
    启动 CLI device-code 登录

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话

    :return CliLoginStartResponse: CLI 登录启动响应
    """

    result = await IdentityService(session, settings).start_cli_login()
    return CliLoginStartResponse(
        data=CliLoginStartData(
            device_code=result.device_code,
            user_code=result.user_code,
            verification_url=result.verification_url,
            expires_in=result.expires_in,
            interval=result.interval,
        ),
        request_id=get_request_id(),
    )


@router.post("/cli/approve", response_model=EmptyResponse)
async def approve_cli_login(
    payload: CliLoginApproveRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    确认 CLI device-code 登录

    :param payload (CliLoginApproveRequest): 确认请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return EmptyResponse: 空响应
    """

    await IdentityService(session, settings).approve_cli_login(
        user=user, user_code=payload.user_code
    )
    return EmptyResponse(request_id=get_request_id())


@router.post("/cli/complete", response_model=AuthTokenResponse)
async def complete_cli_login(
    payload: CliLoginCompleteRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthTokenResponse:
    """
    完成 CLI device-code 登录

    :param payload (CliLoginCompleteRequest): 完成请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话

    :return AuthTokenResponse: 登录令牌响应
    """

    token_issue = await IdentityService(session, settings).complete_cli_login(
        device_code=payload.device_code
    )
    return AuthTokenResponse(data=_token_data(token_issue), request_id=get_request_id())


@router.post("/totp/setup", response_model=TotpSetupResponse)
async def setup_totp(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> TotpSetupResponse:
    """
    设置 TOTP

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return TotpSetupResponse: TOTP 设置响应
    """

    secret = await IdentityService(session, settings).setup_totp(user=user)
    otp_auth_url = (
        f"otpauth://totp/agent-remote:{user.username}?secret={secret}&issuer=agent-remote"
    )
    return TotpSetupResponse(
        data=TotpSetupData(secret=secret, otp_auth_url=otp_auth_url),
        request_id=get_request_id(),
    )


@router.post("/totp/verify", response_model=EmptyResponse)
async def verify_totp(
    payload: TotpVerifyRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    验证并启用 TOTP

    :param payload (TotpVerifyRequest): 验证请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return EmptyResponse: 空响应
    """

    await IdentityService(session, settings).verify_totp(user=user, code=payload.code)
    return EmptyResponse(request_id=get_request_id())
