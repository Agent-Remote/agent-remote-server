from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_node, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import Node
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.connections import (
    VerifyAttachData,
    VerifyAttachRequest,
    VerifyAttachResponse,
)
from agent_remote_server.schemas.nodes import (
    CompleteNodeTaskRequest,
    FailNodeTaskRequest,
    NodeHeartbeatRequest,
    NodeRegisterData,
    NodeRegisterRequest,
    NodeRegisterResponse,
    NodeTaskEnvelope,
    NodeTaskPollData,
    NodeTaskPollResponse,
    ReconcileRequest,
    task_expires_at,
)
from agent_remote_server.services.connections import ConnectionService
from agent_remote_server.services.nodes import NodeService

router = APIRouter(prefix="/node-api", tags=["node-api"])


@router.post("/register", response_model=NodeRegisterResponse)
async def register_node(
    payload: NodeRegisterRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NodeRegisterResponse:
    """
    注册节点

    :param payload (NodeRegisterRequest): 节点注册请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话

    :return NodeRegisterResponse: 节点注册响应
    """

    result = await NodeService(session, settings).register_node(
        node_id=payload.node_id,
        registration_token=payload.registration_token,
        version=payload.version,
    )
    return NodeRegisterResponse(
        data=NodeRegisterData(node_id=result.node.id, node_token=result.raw_node_token),
        request_id=get_request_id(),
    )


@router.post("/heartbeat", response_model=EmptyResponse)
async def heartbeat(
    payload: NodeHeartbeatRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> EmptyResponse:
    """
    提交节点心跳

    :param payload (NodeHeartbeatRequest): 心跳请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return EmptyResponse: 空响应
    """

    await NodeService(session, settings).submit_heartbeat(
        node=node,
        node_id=payload.node_id,
        version=payload.version,
        supported_tool_types=payload.supported_tool_types,
        resources=payload.resources.model_dump(),
        runtime=payload.runtime.model_dump(),
    )
    return EmptyResponse(request_id=get_request_id())


@router.post("/tasks/poll", response_model=NodeTaskPollResponse)
async def poll_tasks(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> NodeTaskPollResponse:
    """
    轮询节点任务

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return NodeTaskPollResponse: 任务轮询响应
    """

    tasks = await NodeService(session, settings).poll_tasks(node=node)
    return NodeTaskPollResponse(
        data=NodeTaskPollData(
            tasks=[
                NodeTaskEnvelope(
                    task_id=task.task_id,
                    node_id=task.node_id,
                    task_type=task.task_type,
                    idempotency_key=task.task_id,
                    payload=task.payload,
                    lease_until=task.lease_until,
                    created_at=task.created_at,
                    expires_at=task_expires_at(task.created_at),
                )
                for task in tasks
                if task.lease_until is not None
            ]
        ),
        request_id=get_request_id(),
    )


@router.post("/tasks/{task_id}/start", response_model=EmptyResponse)
async def start_task(
    task_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> EmptyResponse:
    """
    标记任务开始

    :param task_id (str): 任务 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return EmptyResponse: 空响应
    """

    await NodeService(session, settings).start_task(node=node, task_id=task_id)
    return EmptyResponse(request_id=get_request_id())


@router.post("/tasks/{task_id}/complete", response_model=EmptyResponse)
async def complete_task(
    task_id: str,
    payload: CompleteNodeTaskRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> EmptyResponse:
    """
    完成任务

    :param task_id (str): 任务 ID
    :param payload (CompleteNodeTaskRequest): 完成请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return EmptyResponse: 空响应
    """

    await NodeService(session, settings).complete_task(
        node=node, task_id=task_id, result=payload.result
    )
    return EmptyResponse(request_id=get_request_id())


@router.post("/tasks/{task_id}/fail", response_model=EmptyResponse)
async def fail_task(
    task_id: str,
    payload: FailNodeTaskRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> EmptyResponse:
    """
    标记任务失败

    :param task_id (str): 任务 ID
    :param payload (FailNodeTaskRequest): 失败请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return EmptyResponse: 空响应
    """

    await NodeService(session, settings).fail_task(node=node, task_id=task_id, error=payload.error)
    return EmptyResponse(request_id=get_request_id())


@router.post("/reconcile", response_model=EmptyResponse)
async def reconcile(
    payload: ReconcileRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> EmptyResponse:
    """
    提交节点对账快照

    :param payload (ReconcileRequest): 对账请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return EmptyResponse: 空响应
    """

    await NodeService(session, settings).reconcile(
        node=node,
        node_id=payload.node_id,
        sections=payload.sections,
        snapshot=payload.snapshot,
    )
    return EmptyResponse(request_id=get_request_id())


@router.post("/attach/verify", response_model=VerifyAttachResponse)
async def verify_attach(
    payload: VerifyAttachRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
) -> VerifyAttachResponse:
    """
    校验节点 SSH forced command attach 请求

    :param payload (VerifyAttachRequest): attach 校验请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点

    :return VerifyAttachResponse: attach 校验响应
    """

    tool_session = await ConnectionService(session, settings).verify_node_attach(
        node=node,
        node_id=payload.node_id,
        session_id=payload.session_id,
        device_id=payload.device_id,
    )
    return VerifyAttachResponse(
        data=VerifyAttachData(
            session_id=tool_session.id,
            tmux_session_name=tool_session.tmux_session_name or "",
            container_id=tool_session.container_id,
        ),
        request_id=get_request_id(),
    )
