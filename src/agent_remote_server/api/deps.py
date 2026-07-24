from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.db import create_session_factory
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuthToken, Node, User
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.security import hash_token
from agent_remote_server.services.nodes import NodeService

bearer_scheme = HTTPBearer(auto_error=False)
DEVICE_LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=1)


def get_settings(request: Request) -> Settings:
    """
    获取应用配置

    :param request (Request): 当前请求对象

    :return Settings: 应用配置实例
    """

    return request.app.state.settings


async def get_session(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[AsyncSession]:
    """
    获取请求级数据库会话

    :param request (Request): 当前请求对象
    :param settings (Settings): 应用配置

    :return AsyncIterator: 数据库会话迭代器
    """

    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        session_factory = create_session_factory(settings)
        request.app.state.session_factory = session_factory

    async with session_factory() as session:
        yield session


async def get_current_token(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthToken:
    """
    读取当前认证令牌

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param credentials (HTTPAuthorizationCredentials): Bearer 凭证

    :return AuthToken: 当前令牌记录
    """

    return await _resolve_token(
        settings=settings,
        session=session,
        credentials=credentials,
        allow_expired_device=False,
    )


async def get_refreshable_token(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthToken:
    """
    读取允许轮换的认证令牌，过期设备令牌仍可刷新

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param credentials (HTTPAuthorizationCredentials): Bearer 凭证

    :return AuthToken: 可刷新令牌记录
    """

    return await _resolve_token(
        settings=settings,
        session=session,
        credentials=credentials,
        allow_expired_device=True,
    )


async def _resolve_token(
    *,
    settings: Settings,
    session: AsyncSession,
    credentials: HTTPAuthorizationCredentials | None,
    allow_expired_device: bool,
) -> AuthToken:
    if credentials is None:
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="Authentication is required.",
            status_code=401,
        )

    repository = IdentityRepository(session)
    token_hash = hash_token(settings.secret_key, credentials.credentials)
    token = await repository.get_token_by_hash(token_hash)
    if token is None:
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="Authentication is invalid.",
            status_code=401,
        )

    now = datetime.now(UTC)
    expires_at = (
        token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=UTC)
    )
    if token.status != "active":
        raise ApiError(
            code="AUTH_TOKEN_REVOKED",
            message="Token has been revoked.",
            status_code=401,
        )
    if expires_at <= now and not (allow_expired_device and token.token_type == "device"):
        raise ApiError(
            code="AUTH_TOKEN_EXPIRED",
            message="Token has expired.",
            status_code=401,
        )
    if token.user_device_id is not None:
        device = await repository.get_device(token.user_device_id)
        if device is None or device.status != "active":
            raise ApiError(
                code="DEVICE_REVOKED", message="Device has been revoked.", status_code=403
            )
        last_seen_at = device.last_seen_at
        if last_seen_at is not None and last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=UTC)
        if last_seen_at is None or last_seen_at <= now - DEVICE_LAST_SEEN_WRITE_INTERVAL:
            device.last_seen_at = now
            await session.commit()

    return token


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> User:
    """
    读取当前用户

    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前令牌

    :return User: 当前用户
    """

    user = await IdentityRepository(session).get_user(token.user_id)
    if user is None or user.status != "active":
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="User is not active.",
            status_code=401,
        )
    return user


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    """
    要求当前用户是管理员

    :param user (User): 当前用户

    :return User: 当前管理员
    """

    if user.role != "admin":
        raise ApiError(
            code="COMMON_FORBIDDEN",
            message="Administrator role is required.",
            status_code=403,
        )
    return user


async def get_current_node(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> Node:
    """
    读取当前节点

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param credentials (HTTPAuthorizationCredentials): Bearer 凭证

    :return Node: 当前节点
    """

    if credentials is None:
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="Node credential is required.",
            status_code=401,
        )
    return await NodeService(session, settings).authenticate_node_token(credentials.credentials)
