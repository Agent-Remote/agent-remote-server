from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_token,
    get_current_user,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import AuthToken, Session, User
from agent_remote_server.schemas.connections import AttachSessionData, AttachSessionResponse
from agent_remote_server.schemas.sessions import (
    CreateSessionRequest,
    SessionData,
    SessionListData,
    SessionListResponse,
    SessionResponse,
)
from agent_remote_server.services.connections import ConnectionService
from agent_remote_server.services.sessions import ToolSessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def session_data(tool_session: Session) -> SessionData:
    """
    转换工具 session 响应数据

    :param tool_session (Session): 工具 session 实体

    :return SessionData: session 响应数据
    """

    create_task_id = f"create_tool_session:{tool_session.id}"
    stop_task_id = f"stop_tool_session:{tool_session.id}"
    return SessionData(
        id=tool_session.id,
        tool_type=tool_session.tool_type,
        user_id=tool_session.user_id,
        tool_account_id=tool_session.tool_account_id,
        workspace_id=tool_session.workspace_id,
        node_id=tool_session.node_id,
        project_key=tool_session.project_key,
        status=tool_session.status,
        tmux_session_name=tool_session.tmux_session_name,
        container_id=tool_session.container_id,
        runtime_backend=tool_session.runtime_backend,
        runtime_resource_id=tool_session.runtime_resource_id,
        replaces_session_id=tool_session.replaces_session_id,
        create_task_id=create_task_id,
        stop_task_id=stop_task_id if tool_session.status == "stopping" else None,
        created_at=tool_session.created_at,
        updated_at=tool_session.updated_at,
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    tool_type: Annotated[str | None, Query()] = None,
) -> SessionListResponse:
    """
    列出工具 session

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param tool_type (str): 工具类型过滤

    :return SessionListResponse: session 列表响应
    """

    sessions = await ToolSessionService(session, settings).list_sessions(
        user=user, tool_type=tool_type
    )
    return SessionListResponse(
        data=SessionListData(items=[session_data(item) for item in sessions]),
        request_id=get_request_id(),
    )


@router.post("", response_model=SessionResponse)
async def create_session(
    payload: CreateSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionResponse:
    """
    创建工具运行 session

    :param payload (CreateSessionRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SessionResponse: session 响应
    """

    tool_session = await ToolSessionService(session, settings).create_session(
        user=user,
        tool_type=payload.tool_type,
        tool_account_id=payload.tool_account_id,
        workspace_id=payload.workspace_id,
        project_key=payload.project_key,
        argv=payload.argv,
        replaces_session_id=payload.replaces_session_id,
    )
    return SessionResponse(data=session_data(tool_session), request_id=get_request_id())


@router.get("/current-project", response_model=SessionResponse)
async def get_current_project_session(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    tool_type: Annotated[str, Query()],
    project_key: Annotated[str, Query()],
) -> SessionResponse:
    """
    读取当前项目最近可恢复工具 session

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param tool_type (str): 工具类型
    :param project_key (str): 项目 key

    :return SessionResponse: session 响应
    """

    tool_session = await ToolSessionService(session, settings).get_current_project_session(
        user=user, tool_type=tool_type, project_key=project_key
    )
    return SessionResponse(data=session_data(tool_session), request_id=get_request_id())


@router.get("/{session_id}", response_model=SessionResponse)
async def get_tool_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionResponse:
    """
    读取工具运行 session

    :param session_id (UUID): session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SessionResponse: session 响应
    """

    tool_session = await ToolSessionService(session, settings).get_session(
        user=user, session_id=session_id
    )
    return SessionResponse(data=session_data(tool_session), request_id=get_request_id())


@router.post("/{session_id}/stop", response_model=SessionResponse)
async def stop_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionResponse:
    """
    停止工具运行 session

    :param session_id (UUID): session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return SessionResponse: session 响应
    """

    tool_session = await ToolSessionService(session, settings).stop_session(
        user=user, session_id=session_id
    )
    return SessionResponse(data=session_data(tool_session), request_id=get_request_id())


@router.post("/{session_id}/attach", response_model=AttachSessionResponse)
async def attach_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> AttachSessionResponse:
    """
    创建当前设备的 SSH attach 授权

    :param session_id (UUID): session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前 token

    :return AttachSessionResponse: attach 授权
    """

    authorization = await ConnectionService(session, settings).authorize_attach(
        user=user, token=token, session_id=session_id
    )
    node = authorization.node
    return AttachSessionResponse(
        data=AttachSessionData(
            session_id=authorization.session.id,
            node_id=node.id,
            node_wireguard_ip=node.wireguard_ip or node.ssh_host or "",
            ssh_host=node.wireguard_ip or node.ssh_host or "",
            ssh_port=node.ssh_port or 22,
            ssh_user=node.ssh_user or "agent-remote",
            tmux_session_name=authorization.tmux_session_name,
            command_args=authorization.command_args,
            ssh_command=authorization.ssh_command,
            authorization_task_id=authorization.task_id,
            expires_in=300,
        ),
        request_id=get_request_id(),
    )
