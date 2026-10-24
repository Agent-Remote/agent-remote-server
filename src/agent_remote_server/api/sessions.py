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
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuthToken, Session, User, Workspace
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.connections import AttachSessionData, AttachSessionResponse
from agent_remote_server.schemas.sessions import (
    CreateSessionRequest,
    DeleteSessionRequest,
    SessionCreateRequestData,
    SessionCreateRequestResponse,
    SessionData,
    SessionDeleteRequestData,
    SessionDeleteRequestResponse,
    SessionListData,
    SessionListResponse,
    SessionResponse,
    SessionStopRequestData,
    SessionStopRequestResponse,
    StopSessionRequest,
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


@router.post("", response_model=SessionResponse | SessionCreateRequestResponse)
async def create_session(
    payload: CreateSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionResponse | SessionCreateRequestResponse:
    """
    创建工具运行 session

    :param payload (CreateSessionRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return SessionResponse | SessionCreateRequestResponse: 工具会话响应
    """

    if payload.request_id is not None:
        try:
            existing = await ToolSessionService(session, settings).get_create_request(
                user=user, request_id=payload.request_id
            )
        except ApiError as error:
            if error.code != "SESSION_CREATE_REQUEST_NOT_FOUND":
                raise
        else:
            if existing.observation.get("session_deleted") is True:
                response_data = dict(existing.receipt)
                response_data.update(existing.observation)
                return SessionCreateRequestResponse(
                    data=SessionCreateRequestData.model_validate(response_data),
                    request_id=get_request_id(),
                )

    tool_session = await ToolSessionService(session, settings).create_session(
        user=user,
        tool_type=payload.tool_type,
        tool_account_id=payload.tool_account_id,
        workspace_id=payload.workspace_id,
        project_key=payload.project_key,
        argv=payload.argv,
        replaces_session_id=payload.replaces_session_id,
        request_id=payload.request_id,
        schema_version=payload.schema_version or (1 if payload.request_id is not None else None),
        argv_digest=payload.argv_digest,
        intent_fingerprint=payload.intent_fingerprint,
        sync_target=(
            payload.sync_target.model_dump(mode="json")
            if payload.sync_target is not None
            else (
                {"target_digest": payload.sync_target_digest}
                if payload.sync_target_digest
                else None
            )
        ),
    )
    if payload.request_id is not None:
        accepted = await ToolSessionService(session, settings).get_create_request(
            user=user, request_id=payload.request_id
        )
        response_data = dict(accepted.receipt)
        response_data.update(accepted.observation)
        return SessionCreateRequestResponse(
            data=SessionCreateRequestData.model_validate(response_data), request_id=get_request_id()
        )
    return await session_response(session, tool_session)


@router.get("/requests/{request_id}", response_model=SessionCreateRequestResponse)
async def get_create_request(
    request_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionCreateRequestResponse:
    """
    按所有者和调用方原键读取会话创建受理凭据及当前观察。

    :param request_id (UUID): 调用方生成的原始请求身份
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return SessionCreateRequestResponse: 不可变受理凭据和只读观察
    """
    observation = await ToolSessionService(session, settings).get_create_request(
        user=user, request_id=request_id
    )
    response_data = dict(observation.receipt)
    response_data.update(observation.observation)
    return SessionCreateRequestResponse(
        data=SessionCreateRequestData.model_validate(response_data), request_id=get_request_id()
    )


@router.post("/delete-requests", response_model=SessionDeleteRequestResponse)
async def delete_session_collection_request(
    payload: DeleteSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    ego_browser_revocation_bus: Annotated[
        EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)
    ],
) -> SessionDeleteRequestResponse:
    """
    原子删除用户确认的固定会话集合并保留原键回执。

    :param payload (DeleteSessionRequest): 固定集合删除请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param relay_hub (DeviceRelayHub): 设备 relay 连接中心
    :param ego_browser_revocation_bus (EgoBrowserRevocationPublisher): 浏览器撤销发布器
    :return SessionDeleteRequestResponse: 不可变删除回执及只读观察
    """
    observation = await ToolSessionService(
        session, settings, relay_hub, ego_browser_revocation_bus
    ).delete_session_collection_request(
        user=user,
        request_id=payload.request_id,
        schema_version=payload.schema_version,
        tool_type=payload.tool_type,
        session_ids=payload.session_ids,
        selection=payload.selection.model_dump(mode="json"),
        selection_digest=payload.selection_digest,
        intent_fingerprint=payload.intent_fingerprint,
        reason=payload.reason,
    )
    response_data = dict(observation.receipt)
    response_data.update(observation.observation)
    return SessionDeleteRequestResponse(
        data=SessionDeleteRequestData.model_validate(response_data), request_id=get_request_id()
    )


@router.get("/delete-requests/{request_id}", response_model=SessionDeleteRequestResponse)
async def get_delete_session_collection_request(
    request_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionDeleteRequestResponse:
    """
    按所有者和原键读取固定集合删除回执及当前观察。

    :param request_id (UUID): 调用方原始删除请求标识
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return SessionDeleteRequestResponse: 删除回执和只读观察
    """
    observation = await ToolSessionService(session, settings).get_delete_request(
        user=user, request_id=request_id
    )
    response_data = dict(observation.receipt)
    response_data.update(observation.observation)
    return SessionDeleteRequestResponse(
        data=SessionDeleteRequestData.model_validate(response_data), request_id=get_request_id()
    )


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


@router.post("/{session_id}/stop", response_model=SessionResponse | SessionStopRequestResponse)
async def stop_session(
    session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    ego_browser_revocation_bus: Annotated[
        EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)
    ],
    payload: StopSessionRequest | None = None,
) -> SessionResponse | SessionStopRequestResponse:
    """
    停止工具运行 session

    :param session_id (UUID): 工具会话 ID
    :param payload (StopSessionRequest | None): 可选停止调用方原键请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param relay_hub (DeviceRelayHub): 进程内设备 relay 连接中心
    :param ego_browser_revocation_bus (EgoBrowserRevocationPublisher): 浏览器代次撤销发布器
    :return SessionResponse | SessionStopRequestResponse: 工具会话或停止受理响应
    """

    service = ToolSessionService(session, settings, relay_hub, ego_browser_revocation_bus)
    if payload is not None and payload.request_id is not None:
        observation = await service.stop_session_request(
            user=user,
            session_id=session_id,
            request_id=payload.request_id,
            schema_version=payload.schema_version or 1,
            reason=payload.reason,
            intent_fingerprint=payload.intent_fingerprint,
            expected_session_id=payload.expected_session_id,
            expected_node_id=payload.expected_node_id,
            expected_runtime_backend=payload.expected_runtime_backend,
            expected_stop_task_id=payload.expected_stop_task_id,
        )
        response_data = dict(observation.receipt)
        response_data.update(observation.observation)
        return SessionStopRequestResponse(
            data=SessionStopRequestData.model_validate(response_data),
            request_id=get_request_id(),
        )
    tool_session = await service.stop_session(user=user, session_id=session_id)
    return await session_response(session, tool_session)


@router.get("/stop-requests/{request_id}", response_model=SessionStopRequestResponse)
async def get_stop_request(
    request_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> SessionStopRequestResponse:
    """
    按所有者和原键读取停止受理凭据及当前进度。

    :param request_id (UUID): 调用方生成的停止请求标识
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return SessionStopRequestResponse: 停止受理凭据和只读观察
    """
    observation = await ToolSessionService(session, settings).get_stop_request(
        user=user, request_id=request_id
    )
    response_data = dict(observation.receipt)
    response_data.update(observation.observation)
    return SessionStopRequestResponse(
        data=SessionStopRequestData.model_validate(response_data),
        request_id=get_request_id(),
    )


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
