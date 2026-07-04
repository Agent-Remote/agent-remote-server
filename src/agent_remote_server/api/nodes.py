from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_session, get_settings, require_admin
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import Node, User
from agent_remote_server.schemas.nodes import (
    CreateNodeRequest,
    NodeData,
    NodeListData,
    NodeListResponse,
    NodeRegistrationTokenData,
    NodeRegistrationTokenResponse,
    NodeResponse,
    UpdateNodeRequest,
)
from agent_remote_server.services.nodes import NodeService

router = APIRouter(prefix="/nodes", tags=["nodes"])


def node_data(node: Node) -> NodeData:
    """
    转换节点响应数据

    :param node (Node): 节点实体

    :return NodeData: 节点响应数据
    """

    return NodeData(
        id=node.id,
        name=node.name,
        status=node.status,
        region_code=node.region_code,
        tags=node.tags,
        weight=node.weight,
        wireguard_ip=node.wireguard_ip,
        wireguard_public_key=node.wireguard_public_key,
        wireguard_endpoint=node.wireguard_endpoint,
        ssh_host=node.ssh_host,
        ssh_port=node.ssh_port,
        ssh_user=node.ssh_user,
        supported_tool_types=node.supported_tool_types,
        last_heartbeat_at=node.last_heartbeat_at,
        version=node.version,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


@router.get("", response_model=NodeListResponse)
async def list_nodes(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeListResponse:
    """
    列出节点

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeListResponse: 节点列表响应
    """

    _ = admin
    nodes = await NodeService(session, settings).list_nodes()
    return NodeListResponse(
        data=NodeListData(items=[node_data(node) for node in nodes]),
        request_id=get_request_id(),
    )


@router.post("", response_model=NodeRegistrationTokenResponse)
async def create_node(
    payload: CreateNodeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeRegistrationTokenResponse:
    """
    创建节点

    :param payload (CreateNodeRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeRegistrationTokenResponse: 节点注册 token 响应
    """

    result = await NodeService(session, settings).create_node(
        actor=admin,
        name=payload.name,
        region_code=payload.region_code,
        tags=payload.tags,
        weight=payload.weight,
        supported_tool_types=payload.supported_tool_types,
        wireguard_ip=payload.wireguard_ip,
        wireguard_public_key=payload.wireguard_public_key,
        wireguard_endpoint=payload.wireguard_endpoint,
        ssh_host=payload.ssh_host,
        ssh_port=payload.ssh_port,
        ssh_user=payload.ssh_user,
    )
    return NodeRegistrationTokenResponse(
        data=NodeRegistrationTokenData(
            node=node_data(result.node), registration_token=result.raw_token
        ),
        request_id=get_request_id(),
    )


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(
    node_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeResponse:
    """
    读取节点

    :param node_id (UUID): 节点 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeResponse: 节点响应
    """

    _ = admin
    node = await NodeService(session, settings).get_node(node_id)
    return NodeResponse(data=node_data(node), request_id=get_request_id())


@router.patch("/{node_id}", response_model=NodeResponse)
async def update_node(
    node_id: UUID,
    payload: UpdateNodeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeResponse:
    """
    更新节点

    :param node_id (UUID): 节点 ID
    :param payload (UpdateNodeRequest): 更新请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeResponse: 节点响应
    """

    node = await NodeService(session, settings).update_node(
        actor=admin,
        node_id=node_id,
        name=payload.name,
        status=payload.status,
        tags=payload.tags,
        weight=payload.weight,
        supported_tool_types=payload.supported_tool_types,
        wireguard_ip=payload.wireguard_ip,
        wireguard_public_key=payload.wireguard_public_key,
        wireguard_endpoint=payload.wireguard_endpoint,
        ssh_host=payload.ssh_host,
        ssh_port=payload.ssh_port,
        ssh_user=payload.ssh_user,
    )
    return NodeResponse(data=node_data(node), request_id=get_request_id())


@router.post("/{node_id}/registration-token", response_model=NodeRegistrationTokenResponse)
async def rotate_registration_token(
    node_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeRegistrationTokenResponse:
    """
    轮换节点注册 token

    :param node_id (UUID): 节点 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeRegistrationTokenResponse: 节点注册 token 响应
    """

    result = await NodeService(session, settings).rotate_registration_token(
        actor=admin,
        node_id=node_id,
    )
    return NodeRegistrationTokenResponse(
        data=NodeRegistrationTokenData(
            node=node_data(result.node), registration_token=result.raw_token
        ),
        request_id=get_request_id(),
    )


@router.post("/{node_id}/maintenance", response_model=NodeResponse)
async def set_maintenance(
    node_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeResponse:
    """
    设置节点维护

    :param node_id (UUID): 节点 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeResponse: 节点响应
    """

    node = await NodeService(session, settings).set_maintenance(actor=admin, node_id=node_id)
    return NodeResponse(data=node_data(node), request_id=get_request_id())


@router.post("/{node_id}/disable", response_model=NodeResponse)
async def disable_node(
    node_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> NodeResponse:
    """
    禁用节点

    :param node_id (UUID): 节点 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return NodeResponse: 节点响应
    """

    node = await NodeService(session, settings).disable_node(actor=admin, node_id=node_id)
    return NodeResponse(data=node_data(node), request_id=get_request_id())
