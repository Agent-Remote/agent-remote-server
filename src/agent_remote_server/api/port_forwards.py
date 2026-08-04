from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_node,
    get_current_token,
    get_current_user,
    get_port_forward_token_store,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import AuthToken, Node, PortForward, User
from agent_remote_server.port_forward_tokens import PortForwardTokenStore
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.port_forwards import (
    CreatePortForwardRequest,
    PortForwardConnectionData,
    PortForwardConnectionResponse,
    PortForwardCreatedData,
    PortForwardCreatedResponse,
    PortForwardData,
    PortForwardLeaseData,
    PortForwardLeaseResponse,
    PortForwardListData,
    PortForwardListResponse,
    PortForwardResponse,
    RedeemPortForwardRequest,
    ReleasePortForwardRequest,
    RenewPortForwardRequest,
)
from agent_remote_server.services.port_forwards import PortForwardService, RedeemedPortForward

router = APIRouter(tags=["port-forwards"])
node_router = APIRouter(prefix="/node-api/port-forwards", tags=["node-api"])


def port_forward_data(port_forward: PortForward) -> PortForwardData:
    """
    转换端口转发响应数据

    :param port_forward (PortForward): 端口转发实体

    :return PortForwardData: 端口转发数据
    """

    return PortForwardData(
        id=port_forward.id,
        user_id=port_forward.user_id,
        device_id=port_forward.device_id,
        session_id=port_forward.session_id,
        node_id=port_forward.node_id,
        remote_port=port_forward.remote_port,
        requested_local_port=port_forward.requested_local_port,
        client_instance_id=port_forward.client_instance_id,
        status=port_forward.status,
        bytes_up=port_forward.bytes_up,
        bytes_down=port_forward.bytes_down,
        connection_count=port_forward.connection_count,
        last_connected_at=port_forward.last_connected_at,
        lease_expires_at=port_forward.lease_expires_at,
        expires_at=port_forward.expires_at,
        stopped_at=port_forward.stopped_at,
        stop_reason=port_forward.stop_reason,
        created_at=port_forward.created_at,
        updated_at=port_forward.updated_at,
    )


def lease_data(result: RedeemedPortForward) -> PortForwardLeaseData:
    """
    转换 Node 授权租约数据

    :param result (RedeemedPortForward): 已兑换授权

    :return PortForwardLeaseData: Node 授权租约

    :raises RuntimeError: 已兑换授权缺少运行时租约状态
    """

    runtime_resource_id = result.tool_session.runtime_resource_id
    lease_expires_at = result.port_forward.lease_expires_at
    if runtime_resource_id is None or lease_expires_at is None:
        raise RuntimeError("redeemed port forward is missing runtime lease state")
    return PortForwardLeaseData(
        forward_id=result.port_forward.id,
        session_id=result.tool_session.id,
        runtime_backend=result.tool_session.runtime_backend,
        runtime_resource_id=runtime_resource_id,
        remote_port=result.port_forward.remote_port,
        generation=result.port_forward.connection_generation,
        lease_expires_at=lease_expires_at,
        max_streams=result.policy.max_streams,
        bytes_per_second=result.policy.bytes_per_second,
        control_plane_grace_seconds=result.policy.control_plane_grace_seconds,
    )


def service(
    session: AsyncSession,
    settings: Settings,
    token_store: PortForwardTokenStore,
) -> PortForwardService:
    """
    创建端口转发服务

    :param session (AsyncSession): 数据库会话
    :param settings (Settings): 应用配置
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardService: 端口转发服务
    """

    return PortForwardService(session, settings, token_store)


@router.post(
    "/sessions/{session_id}/port-forwards",
    response_model=PortForwardCreatedResponse,
)
async def create_port_forward(
    session_id: UUID,
    payload: CreatePortForwardRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> PortForwardCreatedResponse:
    """
    创建 session 端口转发

    :param session_id (UUID): 会话 ID
    :param payload (CreatePortForwardRequest): 创建端口转发请求
    :param response (Response): HTTP 响应对象
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前令牌
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardCreatedResponse: 新建端口转发响应

    :raises RuntimeError: 已授权端口转发节点缺少 WireGuard 地址
    """

    result = await service(session, settings, token_store).create(
        user=user,
        token=token,
        session_id=session_id,
        remote_port=payload.remote_port,
        local_port=payload.local_port,
        client_instance_id=payload.client_instance_id,
        ttl_seconds=payload.ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    base = port_forward_data(result.port_forward)
    node_host = result.node.wireguard_ip
    if node_host is None:
        raise RuntimeError("authorized port forward node has no WireGuard address")
    return PortForwardCreatedResponse(
        data=PortForwardCreatedData(
            **base.model_dump(),
            node_wireguard_ip=node_host,
            ssh_user=result.node.ssh_user or "agent-remote",
            ssh_port=result.node.ssh_port or 22,
            connection=PortForwardConnectionData(
                token=result.connection.token,
                expires_at=result.connection.expires_at,
            ),
        ),
        request_id=get_request_id(),
    )


@router.get("/port-forwards", response_model=PortForwardListResponse)
async def list_port_forwards(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
    all_users: Annotated[bool, Query()] = False,
) -> PortForwardListResponse:
    """
    列出 session 端口转发

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token_store (PortForwardTokenStore): 端口转发 token 存储
    :param all_users (bool): 是否列出全部用户的端口转发

    :return PortForwardListResponse: 端口转发列表响应
    """

    values = await service(session, settings, token_store).list(user=user, all_users=all_users)
    return PortForwardListResponse(
        data=PortForwardListData(items=[port_forward_data(value) for value in values]),
        request_id=get_request_id(),
    )


@router.get("/port-forwards/{forward_id}", response_model=PortForwardResponse)
async def get_port_forward(
    forward_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> PortForwardResponse:
    """
    读取 session 端口转发

    :param forward_id (UUID): 端口转发 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardResponse: 端口转发响应
    """

    value = await service(session, settings, token_store).get(user=user, forward_id=forward_id)
    return PortForwardResponse(data=port_forward_data(value), request_id=get_request_id())


@router.post(
    "/port-forwards/{forward_id}/connections",
    response_model=PortForwardConnectionResponse,
)
async def create_port_forward_connection(
    forward_id: UUID,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> PortForwardConnectionResponse:
    """
    签发端口转发重连 token

    :param forward_id (UUID): 端口转发 ID
    :param response (Response): HTTP 响应对象
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前令牌
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardConnectionResponse: 一次性连接凭证响应
    """

    result = await service(session, settings, token_store).issue_connection(
        user=user, token=token, forward_id=forward_id
    )
    response.headers["Cache-Control"] = "no-store"
    return PortForwardConnectionResponse(
        data=PortForwardConnectionData(token=result.token, expires_at=result.expires_at),
        request_id=get_request_id(),
    )


@router.delete("/port-forwards/{forward_id}", response_model=PortForwardResponse)
async def stop_port_forward(
    forward_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> PortForwardResponse:
    """
    停止 session 端口转发

    :param forward_id (UUID): 端口转发 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardResponse: 已停止端口转发响应
    """

    value = await service(session, settings, token_store).stop(user=user, forward_id=forward_id)
    return PortForwardResponse(data=port_forward_data(value), request_id=get_request_id())


@node_router.post("/redeem", response_model=PortForwardLeaseResponse)
async def redeem_port_forward(
    payload: RedeemPortForwardRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> PortForwardLeaseResponse:
    """
    Node 兑换一次性端口转发 token

    :param payload (RedeemPortForwardRequest): 兑换请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardLeaseResponse: Node 授权租约响应
    """

    result = await service(session, settings, token_store).redeem(
        node=node,
        forward_id=payload.forward_id,
        device_id=payload.device_id,
        ssh_key_id=payload.ssh_key_id,
        connect_token=payload.connect_token,
    )
    return PortForwardLeaseResponse(data=lease_data(result), request_id=get_request_id())


@node_router.post("/{forward_id}/renew", response_model=PortForwardLeaseResponse)
async def renew_port_forward(
    forward_id: UUID,
    payload: RenewPortForwardRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> PortForwardLeaseResponse:
    """
    Node 续租端口转发授权

    :param forward_id (UUID): 端口转发 ID
    :param payload (RenewPortForwardRequest): 续租请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return PortForwardLeaseResponse: Node 授权租约响应
    """

    result = await service(session, settings, token_store).renew(
        node=node,
        forward_id=forward_id,
        generation=payload.generation,
        bytes_up_total=payload.bytes_up_total,
        bytes_down_total=payload.bytes_down_total,
        connection_count_total=payload.connection_count_total,
    )
    return PortForwardLeaseResponse(data=lease_data(result), request_id=get_request_id())


@node_router.post("/{forward_id}/release", response_model=EmptyResponse)
async def release_port_forward(
    forward_id: UUID,
    payload: ReleasePortForwardRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    token_store: Annotated[PortForwardTokenStore, Depends(get_port_forward_token_store)],
) -> EmptyResponse:
    """
    Node 释放端口转发连接

    :param forward_id (UUID): 端口转发 ID
    :param payload (ReleasePortForwardRequest): 释放请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前节点
    :param token_store (PortForwardTokenStore): 端口转发 token 存储

    :return EmptyResponse: 空响应
    """

    await service(session, settings, token_store).release(
        node=node,
        forward_id=forward_id,
        generation=payload.generation,
        bytes_up_total=payload.bytes_up_total,
        bytes_down_total=payload.bytes_down_total,
        connection_count_total=payload.connection_count_total,
        reason=payload.reason,
    )
    return EmptyResponse(request_id=get_request_id())
