from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_user, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import Node, SyncSession, User
from agent_remote_server.schemas.workspaces import (
    CreateSyncSessionRequest,
    SyncSessionActionRequest,
    SyncSessionData,
    SyncSessionListData,
    SyncSessionListResponse,
    SyncSessionResponse,
)
from agent_remote_server.services.workspaces import SyncSessionResult, WorkspaceService

router = APIRouter(prefix="/sync-sessions", tags=["sync-sessions"])


def sync_session_data(result: SyncSessionResult) -> SyncSessionData:
    """
    转换同步 session 响应数据

    :param result (SyncSessionResult): 同步 session 结果

    :return SyncSessionData: 同步 session 响应数据
    """

    sync_session = result.sync_session
    return SyncSessionData(
        id=sync_session.id,
        user_id=sync_session.user_id,
        workspace_id=sync_session.workspace_id,
        node_id=sync_session.node_id,
        local_path=sync_session.local_path,
        remote_path=sync_session.remote_path,
        status=sync_session.status,
        conflict_status=sync_session.conflict_status,
        sync_mode=sync_session.sync_mode,
        mutagen_session_id=sync_session.mutagen_session_id,
        remote_endpoint=_remote_endpoint(result.node, sync_session),
        prepare_task_id=result.prepare_task_id,
        created_at=sync_session.created_at,
        updated_at=sync_session.updated_at,
    )


def _remote_endpoint(node: Node | None, sync_session: SyncSession) -> str | None:
    if node is None:
        return None
    host = node.ssh_host or node.wireguard_ip
    if not host:
        return None
    user = node.ssh_user or "agent-remote"
    port = node.ssh_port or 22
    return f"{user}@{host}:{port}:{sync_session.remote_path}"


@router.get("", response_model=SyncSessionListResponse)
async def list_sync_sessions(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionListResponse:
    """
    列出同步 session

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionListResponse: 同步 session 列表响应
    """

    results = await WorkspaceService(session, settings).list_sync_sessions(user=user)
    return SyncSessionListResponse(
        data=SyncSessionListData(items=[sync_session_data(result) for result in results]),
        request_id=get_request_id(),
    )


@router.post("", response_model=SyncSessionResponse)
async def create_sync_session(
    payload: CreateSyncSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionResponse:
    """
    创建同步 session

    :param payload (CreateSyncSessionRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionResponse: 同步 session 响应
    """

    result = await WorkspaceService(session, settings).create_sync_session(
        user=user,
        workspace_id=payload.workspace_id,
        node_id=payload.node_id,
        local_path=payload.local_path,
        sync_mode=payload.sync_mode,
    )
    return SyncSessionResponse(data=sync_session_data(result), request_id=get_request_id())


@router.get("/{sync_session_id}", response_model=SyncSessionResponse)
async def get_sync_session(
    sync_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionResponse:
    """
    读取同步 session

    :param sync_session_id (UUID): 同步 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionResponse: 同步 session 响应
    """

    result = await WorkspaceService(session, settings).get_sync_session(
        user=user, sync_session_id=sync_session_id
    )
    return SyncSessionResponse(data=sync_session_data(result), request_id=get_request_id())


@router.post("/{sync_session_id}/pause", response_model=SyncSessionResponse)
async def pause_sync_session(
    sync_session_id: UUID,
    payload: SyncSessionActionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionResponse:
    """
    暂停同步 session

    :param sync_session_id (UUID): 同步 session ID
    :param payload (SyncSessionActionRequest): 操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionResponse: 同步 session 响应
    """

    _ = payload
    result = await WorkspaceService(session, settings).pause_sync_session(
        user=user, sync_session_id=sync_session_id
    )
    return SyncSessionResponse(data=sync_session_data(result), request_id=get_request_id())


@router.post("/{sync_session_id}/resume", response_model=SyncSessionResponse)
async def resume_sync_session(
    sync_session_id: UUID,
    payload: SyncSessionActionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionResponse:
    """
    恢复同步 session

    :param sync_session_id (UUID): 同步 session ID
    :param payload (SyncSessionActionRequest): 操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionResponse: 同步 session 响应
    """

    _ = payload
    result = await WorkspaceService(session, settings).resume_sync_session(
        user=user, sync_session_id=sync_session_id
    )
    return SyncSessionResponse(data=sync_session_data(result), request_id=get_request_id())


@router.post("/{sync_session_id}/resolve", response_model=SyncSessionResponse)
async def resolve_sync_session(
    sync_session_id: UUID,
    payload: SyncSessionActionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionResponse:
    """
    标记同步冲突已解决

    :param sync_session_id (UUID): 同步 session ID
    :param payload (SyncSessionActionRequest): 操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionResponse: 同步 session 响应
    """

    _ = payload
    result = await WorkspaceService(session, settings).resolve_sync_session(
        user=user, sync_session_id=sync_session_id
    )
    return SyncSessionResponse(data=sync_session_data(result), request_id=get_request_id())


@router.post("/{sync_session_id}/reset", response_model=SyncSessionResponse)
async def reset_sync_session(
    sync_session_id: UUID,
    payload: SyncSessionActionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SyncSessionResponse:
    """
    重置同步 session

    :param sync_session_id (UUID): 同步 session ID
    :param payload (SyncSessionActionRequest): 操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SyncSessionResponse: 同步 session 响应
    """

    _ = payload
    result = await WorkspaceService(session, settings).reset_sync_session(
        user=user, sync_session_id=sync_session_id
    )
    return SyncSessionResponse(data=sync_session_data(result), request_id=get_request_id())
