from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_user, get_session, get_settings, require_admin
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import User
from agent_remote_server.schemas.users import (
    CreateUserRequest,
    UpdateMeRequest,
    UpdateUserRequest,
    UserData,
    UserListData,
    UserListResponse,
    UserResponse,
)
from agent_remote_server.services.identity import IdentityService

router = APIRouter(prefix="/users", tags=["users"])


def _user_data(user: User) -> UserData:
    return UserData(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        totp_enabled=user.totp_enabled,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: Annotated[User, Depends(get_current_user)]) -> UserResponse:
    """
    返回当前用户

    :param user (User): 当前用户

    :return UserResponse: 用户响应
    """

    return UserResponse(data=_user_data(user), request_id=get_request_id())


@router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UpdateMeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> UserResponse:
    """
    更新当前用户

    :param payload (UpdateMeRequest): 更新请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return UserResponse: 用户响应
    """

    updated = await IdentityService(session, settings).update_user(
        actor=user,
        user_id=user.id,
        display_name=payload.display_name,
        status=None,
    )
    return UserResponse(data=_user_data(updated), request_id=get_request_id())


@router.get("", response_model=UserListResponse)
async def list_users(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> UserListResponse:
    """
    列出用户

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return UserListResponse: 用户列表响应
    """

    _ = admin
    users = await IdentityService(session, settings).list_users()
    return UserListResponse(
        data=UserListData(items=[_user_data(user) for user in users]),
        request_id=get_request_id(),
    )


@router.post("", response_model=UserResponse)
async def create_user(
    payload: CreateUserRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> UserResponse:
    """
    创建用户

    :param payload (CreateUserRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return UserResponse: 用户响应
    """

    user = await IdentityService(session, settings).create_user(
        actor=admin,
        username=payload.username,
        password=payload.password,
        role=payload.role,
        display_name=payload.display_name,
    )
    return UserResponse(data=_user_data(user), request_id=get_request_id())


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> UserResponse:
    """
    读取用户

    :param user_id (UUID): 用户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return UserResponse: 用户响应
    """

    _ = admin
    user = await IdentityService(session, settings).get_user(user_id)
    return UserResponse(data=_user_data(user), request_id=get_request_id())


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    payload: UpdateUserRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> UserResponse:
    """
    更新用户

    :param user_id (UUID): 用户 ID
    :param payload (UpdateUserRequest): 更新请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return UserResponse: 用户响应
    """

    user = await IdentityService(session, settings).update_user(
        actor=admin,
        user_id=user_id,
        display_name=payload.display_name,
        status=payload.status,
    )
    return UserResponse(data=_user_data(user), request_id=get_request_id())


@router.post("/{user_id}/disable", response_model=UserResponse)
async def disable_user(
    user_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> UserResponse:
    """
    禁用用户

    :param user_id (UUID): 用户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return UserResponse: 用户响应
    """

    user = await IdentityService(session, settings).disable_user(actor=admin, user_id=user_id)
    return UserResponse(data=_user_data(user), request_id=get_request_id())
