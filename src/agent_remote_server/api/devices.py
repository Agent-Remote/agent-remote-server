from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.auth import _token_data
from agent_remote_server.api.deps import get_current_user, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import User, UserDevice
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.devices import (
    DeviceData,
    DeviceListData,
    DeviceListResponse,
    DeviceRegistrationData,
    DeviceRegistrationResponse,
    DeviceResponse,
    RegisterDeviceRequest,
    RotateDeviceTokenResponse,
)
from agent_remote_server.services.identity import IdentityService

router = APIRouter(prefix="/devices", tags=["devices"])


def _device_data(device: UserDevice) -> DeviceData:
    return DeviceData(
        id=device.id,
        user_id=device.user_id,
        name=device.name,
        platform=device.platform,
        status=device.status,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
    )


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeviceListResponse:
    """
    列出当前用户设备

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return DeviceListResponse: 设备列表响应
    """

    devices = await IdentityService(session, settings).list_devices(user=user)
    return DeviceListResponse(
        data=DeviceListData(items=[_device_data(device) for device in devices]),
        request_id=get_request_id(),
    )


@router.post("", response_model=DeviceRegistrationResponse)
@router.post("/register", response_model=DeviceRegistrationResponse)
async def register_device(
    payload: RegisterDeviceRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeviceRegistrationResponse:
    """
    注册设备

    :param payload (RegisterDeviceRequest): 注册请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return DeviceRegistrationResponse: 设备注册响应
    """

    result = await IdentityService(session, settings).register_device(
        user=user,
        name=payload.name,
        platform=payload.platform,
        ssh_public_key=payload.ssh_public_key,
        wireguard_public_key=payload.wireguard_public_key,
    )
    return DeviceRegistrationResponse(
        data=DeviceRegistrationData(
            device=_device_data(result.device),
            device_token=_token_data(result.token_issue),
            ssh_key_id=result.ssh_key.id,
            wireguard_peer_id=result.wireguard_peer.id if result.wireguard_peer else None,
        ),
        request_id=get_request_id(),
    )


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeviceResponse:
    """
    读取设备

    :param device_id (UUID): 设备 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return DeviceResponse: 设备响应
    """

    device = await IdentityService(session, settings).get_visible_device(
        actor=user, device_id=device_id
    )
    return DeviceResponse(data=_device_data(device), request_id=get_request_id())


@router.post("/{device_id}/disable", response_model=EmptyResponse)
@router.post("/{device_id}/revoke", response_model=EmptyResponse)
async def revoke_device(
    device_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    撤销设备

    :param device_id (UUID): 设备 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return EmptyResponse: 空响应
    """

    await IdentityService(session, settings).revoke_device(actor=user, device_id=device_id)
    return EmptyResponse(request_id=get_request_id())


@router.post("/{device_id}/rotate-token", response_model=RotateDeviceTokenResponse)
async def rotate_device_token(
    device_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> RotateDeviceTokenResponse:
    """
    轮换设备令牌

    :param device_id (UUID): 设备 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return RotateDeviceTokenResponse: 新令牌响应
    """

    token_issue = await IdentityService(session, settings).rotate_device_token(
        actor=user,
        device_id=device_id,
    )
    return RotateDeviceTokenResponse(data=_token_data(token_issue), request_id=get_request_id())
