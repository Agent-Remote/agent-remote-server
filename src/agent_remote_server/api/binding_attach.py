"""
提供固定账户绑定尝试的设备 SSH 授权与只读任务轮询。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_token, get_current_user, get_session
from agent_remote_server.context import get_request_id
from agent_remote_server.models import AuthToken, User
from agent_remote_server.schemas.binding_attach import BindingAttachRequest, BindingAttachResponse
from agent_remote_server.services.binding_attach import BindingAttachService

router = APIRouter()


@router.post("/{tool_account_id}/bind/attach", response_model=BindingAttachResponse)
async def authorize_binding_attach(
    tool_account_id: UUID,
    payload: BindingAttachRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> BindingAttachResponse:
    """
    为当前设备显式授权一个完整绑定尝试。

    :param tool_account_id (UUID): 工具账户身份
    :param payload (BindingAttachRequest): 已确认尝试
    :param session (AsyncSession): 请求事务
    :param user (User): 当前用户
    :param token (AuthToken): 当前设备凭据
    :return BindingAttachResponse: 结构化连接授权
    """
    data = await BindingAttachService(session).resolve(
        user=user,
        token=token,
        account_id=tool_account_id,
        expected_binding_session_id=payload.expected_binding_session_id,
        task_id=None,
    )
    return BindingAttachResponse(data=data, request_id=get_request_id())


@router.get("/{tool_account_id}/bind/attach/{task_id}", response_model=BindingAttachResponse)
async def binding_attach_status(
    tool_account_id: UUID,
    task_id: Annotated[str, Path(min_length=1, max_length=256)],
    expected_binding_session_id: Annotated[
        str, Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> BindingAttachResponse:
    """
    只读观察原授权任务并重新校验完整绑定。

    :param tool_account_id (UUID): 工具账户身份
    :param task_id (str): 原授权任务身份
    :param expected_binding_session_id (str): 原绑定尝试身份
    :param session (AsyncSession): 请求事务
    :param user (User): 当前用户
    :param token (AuthToken): 当前设备凭据
    :return BindingAttachResponse: 原任务与当前授权观察
    """
    data = await BindingAttachService(session).resolve(
        user=user,
        token=token,
        account_id=tool_account_id,
        expected_binding_session_id=expected_binding_session_id,
        task_id=task_id,
    )
    return BindingAttachResponse(data=data, request_id=get_request_id())
