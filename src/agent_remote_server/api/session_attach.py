"""
提供精确 Claude 会话授权、原任务只读观察和节点实际密钥验证。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_device_token_readonly,
    get_current_node,
    get_current_user_readonly,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import AuthToken, Node, User
from agent_remote_server.schemas.session_attach import (
    ExactSessionAttachRequest,
    ExactSessionAttachResponse,
    VerifyExactSessionAttachRequest,
    VerifyExactSessionAttachResponse,
)
from agent_remote_server.schemas.session_attachment import (
    SessionAttachmentAdmissionRequest,
    SessionAttachmentAdmissionResponse,
    SessionAttachmentCleanupRequest,
    SessionAttachmentCleanupResponse,
)
from agent_remote_server.services.session_attach import ExactSessionAttachService
from agent_remote_server.services.session_attachment import SessionAttachmentService

router = APIRouter()


@router.post("/sessions/{session_id}/attach/exact", response_model=ExactSessionAttachResponse)
async def authorize_exact_attach(
    session_id: UUID,
    payload: ExactSessionAttachRequest,
    user: Annotated[User, Depends(get_current_user_readonly)],
    token: Annotated[AuthToken, Depends(get_current_device_token_readonly)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExactSessionAttachResponse:
    """
    为已确认本机设备授权原 Claude 会话。

    :param session_id (UUID): 原会话身份
    :param payload (ExactSessionAttachRequest): 已确认设备身份
    :param user (User): 当前活动用户
    :param token (AuthToken): 当前活动设备令牌
    :param session (AsyncSession): 请求事务
    :param settings (Settings): 应用配置
    :return ExactSessionAttachResponse: 原授权任务与连接字段
    """
    data = await ExactSessionAttachService(session, settings).resolve(
        user, token, session_id, payload.expected_device_id
    )
    return ExactSessionAttachResponse(data=data, request_id=get_request_id())


@router.get(
    "/sessions/{session_id}/attach/exact/{authorization_task_id}",
    response_model=ExactSessionAttachResponse,
)
async def observe_exact_attach(
    session_id: UUID,
    authorization_task_id: Annotated[str, Path(min_length=1, max_length=256)],
    expected_device_id: Annotated[UUID, Query()],
    expected_task_record_id: Annotated[UUID, Query()],
    user: Annotated[User, Depends(get_current_user_readonly)],
    token: Annotated[AuthToken, Depends(get_current_device_token_readonly)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExactSessionAttachResponse:
    """
    只读重验当前权限并观察原任务数据库行。

    :param session_id (UUID): 原会话身份
    :param authorization_task_id (str): 原逻辑任务身份
    :param expected_device_id (UUID): 原本机设备身份
    :param expected_task_record_id (UUID): 原任务数据库行身份
    :param user (User): 当前活动用户
    :param token (AuthToken): 当前活动设备令牌
    :param session (AsyncSession): 只读请求事务
    :param settings (Settings): 应用配置
    :return ExactSessionAttachResponse: 当前授权与原任务状态
    """
    data = await ExactSessionAttachService(session, settings).resolve(
        user, token, session_id, expected_device_id, authorization_task_id, expected_task_record_id
    )
    return ExactSessionAttachResponse(data=data, request_id=get_request_id())


@router.post("/node-api/attach/exact/verify", response_model=VerifyExactSessionAttachResponse)
async def verify_exact_attach(
    payload: VerifyExactSessionAttachRequest,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VerifyExactSessionAttachResponse:
    """
    为强制命令实际认证设备及密钥重新验证当前运行时。

    :param payload (VerifyExactSessionAttachRequest): 四个强制命令身份
    :param node (Node): 当前认证节点
    :param session (AsyncSession): 请求事务
    :param settings (Settings): 应用配置
    :return VerifyExactSessionAttachResponse: 精确身份回显与运行时投影
    """
    data = await ExactSessionAttachService(session, settings).verify(node, payload)
    return VerifyExactSessionAttachResponse(data=data, request_id=get_request_id())


@router.post(
    "/node-api/attach/exact/attachments/verify",
    response_model=SessionAttachmentAdmissionResponse,
)
async def verify_exact_attachment_transfer(
    payload: SessionAttachmentAdmissionRequest,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionAttachmentAdmissionResponse:
    """
    为 Node 网关签发绑定会话、设备和实际 SSH 密钥的附件租约。

    :param payload (SessionAttachmentAdmissionRequest): Node 提交的严格身份
    :param node (Node): 当前认证节点
    :param session (AsyncSession): 请求事务
    :return SessionAttachmentAdmissionResponse: 不含账户路径的租约
    """

    data = await SessionAttachmentService(session).admit(node, payload)
    return SessionAttachmentAdmissionResponse(data=data, request_id=get_request_id())


@router.post(
    "/node-api/attach/exact/attachments/cleanup",
    response_model=SessionAttachmentCleanupResponse,
)
async def record_exact_attachment_cleanup(
    payload: SessionAttachmentCleanupRequest,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SessionAttachmentCleanupResponse:
    """
    持久化 Node 返回的严格清理回执并保留未知结果。

    :param payload (SessionAttachmentCleanupRequest): Node 清理回执
    :param node (Node): 当前认证节点
    :param session (AsyncSession): 请求事务
    :return SessionAttachmentCleanupResponse: 持久化回执
    """

    data = await SessionAttachmentService(session).record_cleanup(node, payload)
    return SessionAttachmentCleanupResponse(data=data, request_id=get_request_id())
