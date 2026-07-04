from typing import Annotated
from uuid import UUID

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
from agent_remote_server.models import AuthToken, User, Workspace
from agent_remote_server.schemas.workspaces import (
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
    WorkspaceData,
    WorkspaceListData,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from agent_remote_server.services.workspaces import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def workspace_data(workspace: Workspace) -> WorkspaceData:
    """
    转换 workspace 响应数据

    :param workspace (Workspace): workspace 实体

    :return WorkspaceData: workspace 响应数据
    """

    return WorkspaceData(
        id=workspace.id,
        user_id=workspace.user_id,
        device_id=workspace.device_id,
        project_key=workspace.project_key,
        local_start_path=workspace.local_start_path,
        display_name=workspace.display_name,
        remote_path=workspace.remote_path,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


@router.get("", response_model=WorkspaceListResponse)
async def list_workspaces(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> WorkspaceListResponse:
    """
    列出 workspace

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return WorkspaceListResponse: workspace 列表响应
    """

    workspaces = await WorkspaceService(session, settings).list_workspaces(user=user)
    return WorkspaceListResponse(
        data=WorkspaceListData(items=[workspace_data(workspace) for workspace in workspaces]),
        request_id=get_request_id(),
    )


@router.post("", response_model=WorkspaceResponse)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> WorkspaceResponse:
    """
    创建 workspace

    :param payload (CreateWorkspaceRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前令牌

    :return WorkspaceResponse: workspace 响应
    """

    workspace = await WorkspaceService(session, settings).create_workspace(
        user=user,
        token=token,
        device_id=payload.device_id,
        project_key=payload.project_key,
        local_start_path=payload.local_start_path,
        display_name=payload.display_name,
    )
    return WorkspaceResponse(data=workspace_data(workspace), request_id=get_request_id())


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> WorkspaceResponse:
    """
    读取 workspace

    :param workspace_id (UUID): workspace ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return WorkspaceResponse: workspace 响应
    """

    workspace = await WorkspaceService(session, settings).get_workspace(
        user=user, workspace_id=workspace_id
    )
    return WorkspaceResponse(data=workspace_data(workspace), request_id=get_request_id())


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    payload: UpdateWorkspaceRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> WorkspaceResponse:
    """
    更新 workspace

    :param workspace_id (UUID): workspace ID
    :param payload (UpdateWorkspaceRequest): 更新请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return WorkspaceResponse: workspace 响应
    """

    workspace = await WorkspaceService(session, settings).update_workspace(
        user=user,
        workspace_id=workspace_id,
        local_start_path=payload.local_start_path,
        display_name=payload.display_name,
    )
    return WorkspaceResponse(data=workspace_data(workspace), request_id=get_request_id())
