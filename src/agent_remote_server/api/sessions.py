"""
提供会话 API。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_token,
    get_current_user,
    get_device_relay_hub,
    get_ego_browser_revocation_bus,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.ego_browser.relay import EgoBrowserRevocationPublisher
from agent_remote_server.models import AuthToken, Session, User, Workspace
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.connections import AttachSessionData, AttachSessionResponse
from agent_remote_server.schemas.sessions import (
    CreateSessionRequest,
    SessionData,
    SessionListData,
    SessionListResponse,
    SessionResponse,
)
from agent_remote_server.schemas.skill_stop_status import SkillStopStatusResponse
from agent_remote_server.schemas.skill_takeover_status import SkillTakeoverStatusResponse
from agent_remote_server.services.connections import ConnectionService
from agent_remote_server.services.sessions import ToolSessionService
from agent_remote_server.services.skills.stop_status import SkillStopStatusService
from agent_remote_server.services.skills.takeover_status import SkillTakeoverStatusService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def session_data(tool_session: Session, workspace: Workspace | None = None) -> SessionData:
    """
    转换工具 session 响应数据

    :param tool_session (Session): 工具 session 实体
    :param workspace (Workspace | None): session 对应工作区
    :return SessionData: 工具会话响应数据
    """

    create_task_id = f"create_tool_session:{tool_session.id}"
    stop_task_id = f"stop_tool_session:{tool_session.id}"
    return SessionData(
        id=tool_session.id,
        tool_type=tool_session.tool_type,
        user_id=tool_session.user_id,
        tool_account_id=tool_session.tool_account_id,
        workspace_id=tool_session.workspace_id,
        workspace_local_path=workspace.local_start_path if workspace is not None else None,
        workspace_remote_path=workspace.remote_path if workspace is not None else None,
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


async def session_response(session: AsyncSession, tool_session: Session) -> SessionResponse:
    """
    单会话响应补充原始保存身份，不按当前账户模式推断受管状态。

    :param session (AsyncSession): 请求数据库会话
    :param tool_session (Session): 已按当前用户授权的会话
    :return SessionResponse: 包含可持久查询操作身份的会话响应
    """
    data = session_data(tool_session)
    snapshot = await SkillRuntimeRepository(session).snapshot_for_session(
        tool_session.user_id, tool_session.id
    )
    data.skill_finalization_operation_id = snapshot.id if snapshot is not None else None
    return SessionResponse(data=data, request_id=get_request_id())


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    tool_type: Annotated[str | None, Query()] = None,
    statuses: Annotated[list[str] | None, Query(alias="status")] = None,
) -> SessionListResponse:
    """
    列出工具 session

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param tool_type (str | None): 工具类型过滤
    :param statuses (list[str] | None): 工具会话状态过滤
    :return SessionListResponse: 工具会话列表响应
    """

    sessions = await ToolSessionService(session, settings).list_sessions(
        user=user, tool_type=tool_type, statuses=statuses
    )
    return SessionListResponse(
        data=SessionListData(
            items=[session_data(tool_session, workspace) for tool_session, workspace in sessions]
        ),
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
    :return SessionResponse: 工具会话响应
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
    return await session_response(session, tool_session)


@router.delete("", response_model=EmptyResponse)
async def delete_inactive_sessions(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    ego_browser_revocation_bus: Annotated[
        EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)
    ],
) -> EmptyResponse:
    """
    删除当前用户全部已停止、已中断和失败的工具 session

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param relay_hub (DeviceRelayHub): 进程内设备 relay 连接中心
    :param ego_browser_revocation_bus (EgoBrowserRevocationPublisher): 浏览器代次撤销发布器
    :return EmptyResponse: 空响应
    """

    await ToolSessionService(
        session, settings, relay_hub, ego_browser_revocation_bus
    ).delete_inactive_sessions(user=user)
    return EmptyResponse(request_id=get_request_id())


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
    :return SessionResponse: 工具会话响应
    """

    tool_session = await ToolSessionService(session, settings).get_current_project_session(
        user=user, tool_type=tool_type, project_key=project_key
    )
    return await session_response(session, tool_session)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_tool_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionResponse:
    """
    读取工具运行 session

    :param session_id (UUID): 工具会话 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return SessionResponse: 工具会话响应
    """

    tool_session = await ToolSessionService(session, settings).get_session(
        user=user, session_id=session_id
    )
    return await session_response(session, tool_session)


@router.delete("/{session_id}", response_model=EmptyResponse)
async def delete_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    ego_browser_revocation_bus: Annotated[
        EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)
    ],
) -> EmptyResponse:
    """
    删除已停止或已中断工具 session

    :param session_id (UUID): 工具会话 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param relay_hub (DeviceRelayHub): 进程内设备 relay 连接中心
    :param ego_browser_revocation_bus (EgoBrowserRevocationPublisher): 浏览器代次撤销发布器
    :return EmptyResponse: 空响应
    """

    await ToolSessionService(
        session, settings, relay_hub, ego_browser_revocation_bus
    ).delete_session(user=user, session_id=session_id)
    return EmptyResponse(request_id=get_request_id())


@router.post("/{session_id}/stop", response_model=SessionResponse)
async def stop_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    ego_browser_revocation_bus: Annotated[
        EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)
    ],
) -> SessionResponse:
    """
    停止工具运行 session

    :param session_id (UUID): 工具会话 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param relay_hub (DeviceRelayHub): 进程内设备 relay 连接中心
    :param ego_browser_revocation_bus (EgoBrowserRevocationPublisher): 浏览器代次撤销发布器
    :return SessionResponse: 工具会话响应
    """

    tool_session = await ToolSessionService(
        session, settings, relay_hub, ego_browser_revocation_bus
    ).stop_session(user=user, session_id=session_id)
    return await session_response(session, tool_session)


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

    :param session_id (UUID): 工具会话 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前 token
    :return AttachSessionResponse: 挂接授权
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
            forward_ssh_agent=authorization.forward_ssh_agent,
            authorization_task_id=authorization.task_id,
            authorization_task_status=authorization.task_status,
            expires_in=300,
        ),
        request_id=get_request_id(),
    )


@router.get("/skill-finalizations/{operation_id}", response_model=SkillStopStatusResponse)
async def skill_finalization_status(
    operation_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SkillStopStatusResponse:
    """
    使用现有会话用户或设备认证查询删除后仍保留的收尾操作。

    :param operation_id (UUID): 原始快照兼收尾操作身份
    :param session (AsyncSession): 请求数据库会话
    :param user (User): 当前认证用户
    :return SkillStopStatusResponse: 不包含文件内容的保存状态
    """
    data = await SkillStopStatusService(session).read(user.id, operation_id)
    return SkillStopStatusResponse(data=data, request_id=get_request_id())


@router.get("/skill-takeovers/{operation_id}", response_model=SkillTakeoverStatusResponse)
async def skill_takeover_status(
    operation_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SkillTakeoverStatusResponse:
    """
    现有会话用户或设备可查询原始接管，不获取节点清单或创建新预约。

    :param operation_id (UUID): 原始接管身份
    :param session (AsyncSession): 请求数据库事务
    :param user (User): 当前认证用户
    :return SkillTakeoverStatusResponse: 有界只读接管进度
    """
    data = await SkillTakeoverStatusService(session).read(user.id, operation_id)
    return SkillTakeoverStatusResponse(data=data, request_id=get_request_id())
