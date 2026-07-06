from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_user, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import DeveloperCredentialProfile, User
from agent_remote_server.schemas.developer_credentials import (
    BindDeveloperCredentialProfileRequest,
    CreateDeveloperCredentialProfileRequest,
    DeveloperCredentialProfileData,
    DeveloperCredentialProfileListData,
    DeveloperCredentialProfileListResponse,
    DeveloperCredentialProfileResponse,
    UpdateDeveloperCredentialProfileRequest,
)
from agent_remote_server.services.developer_credentials import DeveloperCredentialService

router = APIRouter(prefix="/developer-credential-profiles", tags=["developer-credentials"])


def profile_data(profile: DeveloperCredentialProfile) -> DeveloperCredentialProfileData:
    """
    转换开发凭据 profile 响应数据
    """

    return DeveloperCredentialProfileData(
        id=profile.id,
        user_id=profile.user_id,
        display_name=profile.display_name,
        status=profile.status,
        git_identity=profile.git_identity,
        github_cli_mode=profile.github_cli_mode,
        ssh_mode=profile.ssh_mode,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("", response_model=DeveloperCredentialProfileListResponse)
async def list_profiles(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileListResponse:
    """
    列出开发凭据 profile
    """

    profiles = await DeveloperCredentialService(session, settings).list_profiles(user=user)
    return DeveloperCredentialProfileListResponse(
        data=DeveloperCredentialProfileListData(
            items=[profile_data(profile) for profile in profiles]
        ),
        request_id=get_request_id(),
    )


@router.post("", response_model=DeveloperCredentialProfileResponse)
async def create_profile(
    payload: CreateDeveloperCredentialProfileRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileResponse:
    """
    创建开发凭据 profile
    """

    profile = await DeveloperCredentialService(session, settings).create_profile(
        user=user,
        display_name=payload.display_name,
        git_identity=payload.git_identity.model_dump(exclude_none=True),
        github_cli_mode=payload.github_cli.mode,
        ssh_mode=payload.ssh.mode,
    )
    return DeveloperCredentialProfileResponse(
        data=profile_data(profile), request_id=get_request_id()
    )


@router.get("/{profile_id}", response_model=DeveloperCredentialProfileResponse)
async def get_profile(
    profile_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileResponse:
    """
    读取开发凭据 profile
    """

    profile = await DeveloperCredentialService(session, settings).get_profile(
        user=user,
        profile_id=profile_id,
    )
    return DeveloperCredentialProfileResponse(
        data=profile_data(profile), request_id=get_request_id()
    )


@router.patch("/{profile_id}", response_model=DeveloperCredentialProfileResponse)
async def update_profile(
    profile_id: UUID,
    payload: UpdateDeveloperCredentialProfileRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileResponse:
    """
    更新开发凭据 profile
    """

    profile = await DeveloperCredentialService(session, settings).update_profile(
        user=user,
        profile_id=profile_id,
        display_name=payload.display_name,
        status=payload.status,
        git_identity=payload.git_identity.model_dump(exclude_none=True)
        if payload.git_identity is not None
        else None,
        github_cli_mode=payload.github_cli.mode if payload.github_cli is not None else None,
        ssh_mode=payload.ssh.mode if payload.ssh is not None else None,
    )
    return DeveloperCredentialProfileResponse(
        data=profile_data(profile), request_id=get_request_id()
    )


@router.post("/{profile_id}/disable", response_model=DeveloperCredentialProfileResponse)
async def disable_profile(
    profile_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileResponse:
    """
    禁用开发凭据 profile
    """

    profile = await DeveloperCredentialService(session, settings).disable_profile(
        user=user,
        profile_id=profile_id,
    )
    return DeveloperCredentialProfileResponse(
        data=profile_data(profile), request_id=get_request_id()
    )


@router.post(
    "/tool-accounts/{tool_account_id}/bind", response_model=DeveloperCredentialProfileResponse
)
async def bind_profile(
    tool_account_id: UUID,
    payload: BindDeveloperCredentialProfileRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileResponse:
    """
    绑定开发凭据 profile 到工具账户
    """

    profile = await DeveloperCredentialService(session, settings).bind_to_tool_account(
        user=user,
        account_id=tool_account_id,
        profile_id=payload.profile_id,
    )
    return DeveloperCredentialProfileResponse(
        data=profile_data(profile), request_id=get_request_id()
    )


@router.delete("/tool-accounts/{tool_account_id}/bind", status_code=204)
async def unbind_profile(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """
    解除工具账户开发凭据 profile 绑定
    """

    await DeveloperCredentialService(session, settings).unbind_from_tool_account(
        user=user,
        account_id=tool_account_id,
    )
    return Response(status_code=204)
