from typing import Annotated, cast

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
from agent_remote_server.models import AuthToken, Node, User, WireGuardPeer
from agent_remote_server.schemas.connections import (
    EnrollWireGuardPeerData,
    EnrollWireGuardPeerRequest,
    EnrollWireGuardPeerResponse,
    WireGuardConfigData,
    WireGuardConfigResponse,
    WireGuardNodePeerData,
)
from agent_remote_server.services.connections import ConnectionService

router = APIRouter(prefix="/network", tags=["network"])


def _wireguard_node_peer(node: Node) -> WireGuardNodePeerData:
    return WireGuardNodePeerData(
        node_id=node.id,
        name=node.name,
        region_code=node.region_code,
        public_key=node.wireguard_public_key or "",
        endpoint=node.wireguard_endpoint or "",
        allowed_ips=[f"{node.wireguard_ip}/32"] if node.wireguard_ip else [],
        persistent_keepalive_seconds=25,
    )


@router.post("/wireguard/peer", response_model=EnrollWireGuardPeerResponse)
async def enroll_wireguard_peer(
    payload: EnrollWireGuardPeerRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> EnrollWireGuardPeerResponse:
    """
    为当前设备登记或更新 WireGuard 公钥

    :param payload (EnrollWireGuardPeerRequest): WireGuard 公钥请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前设备令牌

    :return EnrollWireGuardPeerResponse: peer 登记结果
    """

    result = await ConnectionService(session, settings).enroll_wireguard_peer(
        user=user, token=token, public_key=payload.public_key
    )
    return EnrollWireGuardPeerResponse(
        data=EnrollWireGuardPeerData(
            device_id=result.device.id,
            peer_id=result.peer.id,
            interface_address=result.peer.ip_address,
        ),
        request_id=get_request_id(),
    )


@router.get("/wireguard/config", response_model=WireGuardConfigResponse)
async def get_wireguard_config(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> WireGuardConfigResponse:
    """
    读取当前设备 WireGuard 配置

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前 token

    :return WireGuardConfigResponse: WireGuard 配置
    """

    result = await ConnectionService(session, settings).get_wireguard_config(user=user, token=token)
    peer = cast(WireGuardPeer, result["peer"])
    assert peer.user_device_id is not None
    nodes = cast(list[Node], result["nodes"])
    return WireGuardConfigResponse(
        data=WireGuardConfigData(
            device_id=peer.user_device_id,
            interface_address=peer.ip_address,
            private_key_ref="agent-remote local WireGuard private key",
            dns=[],
            peers=[_wireguard_node_peer(node) for node in nodes if isinstance(node, Node)],
        ),
        request_id=get_request_id(),
    )
