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
from agent_remote_server.models import AuthToken, User
from agent_remote_server.schemas.connections import AttachSessionData, AttachSessionResponse
from agent_remote_server.services.connections import ConnectionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


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
