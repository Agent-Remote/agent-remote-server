from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.db import create_session_factory
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.device_control.relay_store import DeviceRelayStore
from agent_remote_server.device_control.release import (
    DeviceControlReleaseEvidence,
    DeviceControlReleaseEvidenceError,
    ensure_device_control_release_evidence_current,
)
from agent_remote_server.ego_browser.relay import (
    EgoBrowserRelayHub,
    EgoBrowserRelayStore,
    EgoBrowserRevocationPublisher,
)
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuthToken,
    EgoBrowserDevice,
    EgoBrowserDeviceCredential,
    Node,
    User,
)
from agent_remote_server.port_forwarding.tokens import PortForwardTokenStore
from agent_remote_server.repositories.ego_browser import EgoBrowserRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.security import hash_token
from agent_remote_server.services.nodes import NodeService

bearer_scheme = HTTPBearer(auto_error=False)
DEVICE_LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class EgoBrowserDeviceAuth:
    """已验证的独立 ego-browser 设备客户端身份。"""

    user: User
    device: EgoBrowserDevice
    credential: EgoBrowserDeviceCredential


@dataclass(frozen=True)
class EgoBrowserPrincipal:
    """已验证的 ego-browser 用户或独立 Device Client 身份。"""

    user: User
    device_auth: EgoBrowserDeviceAuth | None


def get_settings(request: Request) -> Settings:
    """
    获取应用配置

    :param request (Request): 当前请求对象

    :return Settings: 应用配置实例
    """

    return request.app.state.settings


def get_port_forward_token_store(request: Request) -> PortForwardTokenStore:
    """
    获取一次性端口转发 token store

    :param request (Request): 当前请求对象

    :return PortForwardTokenStore: 端口转发令牌存储
    """

    return request.app.state.port_forward_token_store


def get_device_relay_store(request: Request) -> DeviceRelayStore:
    """
    获取设备中继短期状态存储

    :param request (Request): 当前请求对象

    :return DeviceRelayStore: 设备中继短期状态存储
    """

    return request.app.state.device_relay_store


def get_device_relay_hub(request: Request) -> DeviceRelayHub:
    """
    获取设备密文 relay 的进程内连接中心

    :param request (Request): 当前请求

    :return DeviceRelayHub: 设备密文 relay 连接中心
    """

    return request.app.state.device_relay_hub


def get_ego_browser_relay_store(request: Request) -> EgoBrowserRelayStore:
    """
    获取独立 ego-browser relay 一次性票据存储。

    :param request (Request): 当前 HTTP 请求上下文

    :return EgoBrowserRelayStore: 按当前部署模式创建的短期状态存储
    """

    return request.app.state.ego_browser_relay_store


def get_ego_browser_relay_hub(request: Request) -> EgoBrowserRelayHub:
    """
    获取独立 ego-browser relay 连接中心。

    :param request (Request): 当前 HTTP 请求上下文

    :return EgoBrowserRelayHub: 按当前部署模式创建的 relay 连接中心
    """

    return request.app.state.ego_browser_relay_hub


def get_ego_browser_revocation_bus(request: Request) -> EgoBrowserRevocationPublisher:
    """
    获取独立 ego-browser relay 撤销总线。

    :param request (Request): 当前 HTTP 请求上下文

    :return EgoBrowserRevocationPublisher: 应用配置的 ego-browser 撤销发布器
    """

    return request.app.state.ego_browser_revocation_bus


def require_current_device_control_release(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """
    拒绝缺少已验证生产发布证据的设备控制推进操作

    :param request (Request): 当前请求对象
    :param settings (Settings): 应用配置

    :raises ApiError: 生产发布证据缺失、尚未生效或旧版证据已经过期
    """

    evidence: DeviceControlReleaseEvidence | None = getattr(
        request.app.state,
        "device_control_release_evidence",
        None,
    )
    try:
        ensure_device_control_release_evidence_current(
            environment=settings.environment,
            enabled=settings.device_control_enabled,
            authorization_mode=settings.device_session_authorization_mode,
            evidence=evidence,
        )
    except DeviceControlReleaseEvidenceError as exc:
        raise ApiError(
            code="DEVICE_CONTROL_RELEASE_EVIDENCE_EXPIRED",
            message="Device control release approval is unavailable or expired.",
            status_code=503,
        ) from exc


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

    :raises ApiError: 令牌所属用户不存在或未处于启用状态
    """

    user = await IdentityRepository(session).get_user(token.user_id)
    if user is None or user.status != "active":
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="User is not active.",
            status_code=401,
        )
    return user


async def get_ego_browser_device_auth(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> EgoBrowserDeviceAuth:
    """
    解析独立 ego-browser Device Client 凭据并返回设备身份。

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param credentials (HTTPAuthorizationCredentials | None): 请求携带的可选 Bearer 凭据

    :return EgoBrowserDeviceAuth: 通过校验的独立设备认证上下文

    :raises ApiError: 请求未携带独立设备凭据或凭据无效
    """

    if credentials is None or not credentials.credentials:
        raise ApiError(
            code="EGO_BROWSER_CREDENTIAL_REQUIRED",
            message="An ego-browser device credential is required.",
            status_code=401,
        )
    return await _resolve_ego_browser_device_auth(
        settings=settings,
        session=session,
        raw_token=credentials.credentials,
        touch=True,
    )


async def get_ego_browser_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> EgoBrowserPrincipal:
    """
    解析 ego-browser 接口支持的用户或独立设备身份。

    独立设备凭据带有不可混淆的 ``egbc_`` 前缀；一旦请求使用该前缀，
    任何失效都返回设备凭据错误，而不会回退为普通用户令牌。

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param credentials (HTTPAuthorizationCredentials | None): 请求携带的可选 Bearer 凭据

    :return EgoBrowserPrincipal: 通过校验的用户或独立设备认证主体

    :raises ApiError: 请求未认证或凭据对应的用户不可用
    """

    if credentials is None or not credentials.credentials:
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="Authentication is required.",
            status_code=401,
        )
    raw_token = credentials.credentials
    if raw_token.startswith("egbc_"):
        auth = await _resolve_ego_browser_device_auth(
            settings=settings,
            session=session,
            raw_token=raw_token,
            touch=True,
        )
        return EgoBrowserPrincipal(user=auth.user, device_auth=auth)
    token = await _resolve_token(
        settings=settings,
        session=session,
        credentials=credentials,
        allow_expired_device=False,
    )
    user = await IdentityRepository(session).get_user(token.user_id)
    if user is None or user.status != "active":
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="User is not active.",
            status_code=401,
        )
    return EgoBrowserPrincipal(user=user, device_auth=None)


async def get_ego_browser_user_or_device_auth(
    principal: Annotated[EgoBrowserPrincipal, Depends(get_ego_browser_principal)],
) -> User:
    """
    返回 ego-browser 只读接口使用的已验证用户。

    :param principal (EgoBrowserPrincipal): 当前认证的用户或独立设备主体

    :return User: 认证主体所属用户
    """

    return principal.user


async def _resolve_ego_browser_device_auth(
    *,
    settings: Settings,
    session: AsyncSession,
    raw_token: str,
    touch: bool,
) -> EgoBrowserDeviceAuth:
    """解析并校验独立设备凭据的状态、代际和所属用户。"""

    repository = EgoBrowserRepository(session)
    credential = await repository.get_device_credential_by_hash(
        hash_token(settings.secret_key, raw_token),
        for_update=True,
    )
    if credential is None:
        raise ApiError(
            code="EGO_BROWSER_CREDENTIAL_INVALID",
            message="The ego-browser device credential is invalid.",
            status_code=401,
        )
    now = datetime.now(UTC)
    expires_at = (
        credential.expires_at
        if credential.expires_at.tzinfo
        else credential.expires_at.replace(tzinfo=UTC)
    )
    if credential.status != "active":
        raise ApiError(
            code="EGO_BROWSER_CREDENTIAL_REVOKED",
            message="The ego-browser device credential has been revoked.",
            status_code=401,
        )
    if expires_at <= now:
        credential.status = "expired"
        await session.commit()
        raise ApiError(
            code="EGO_BROWSER_CREDENTIAL_EXPIRED",
            message="The ego-browser device credential has expired.",
            status_code=401,
        )
    device = await repository.get_device(credential.ego_browser_device_id)
    if (
        device is None
        or device.user_id != credential.user_id
        or device.status != "active"
        or device.generation != credential.generation
    ):
        credential.status = "revoked"
        credential.revoked_at = now
        await session.commit()
        raise ApiError(
            code="EGO_BROWSER_CREDENTIAL_REVOKED",
            message="The ego-browser device is no longer active.",
            status_code=403,
        )
    user = await IdentityRepository(session).get_user(credential.user_id)
    if user is None or user.status != "active":
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="User is not active.",
            status_code=401,
        )
    if touch:
        last_used_at = credential.last_used_at
        if last_used_at is not None and last_used_at.tzinfo is None:
            last_used_at = last_used_at.replace(tzinfo=UTC)
        last_seen_at = device.last_seen_at
        if last_seen_at is not None and last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=UTC)
        should_update = (
            last_used_at is None
            or last_used_at <= now - DEVICE_LAST_SEEN_WRITE_INTERVAL
            or last_seen_at is None
            or last_seen_at <= now - DEVICE_LAST_SEEN_WRITE_INTERVAL
        )
        if should_update:
            credential.last_used_at = now
            device.last_seen_at = now
            await session.commit()
    return EgoBrowserDeviceAuth(user=user, device=device, credential=credential)


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    """
    要求当前用户是管理员

    :param user (User): 当前用户

    :return User: 当前管理员

    :raises ApiError: 当前用户不是管理员
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

    :raises ApiError: 请求未携带有效的 Node 凭据
    """

    if credentials is None:
        raise ApiError(
            code="COMMON_UNAUTHORIZED",
            message="Node credential is required.",
            status_code=401,
        )
    return await NodeService(session, settings).authenticate_node_token(credentials.credentials)
