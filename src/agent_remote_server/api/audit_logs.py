from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_user, get_session
from agent_remote_server.context import get_request_id
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, User
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.schemas.audit import (
    AuditLogData,
    AuditLogListData,
    AuditLogListResponse,
    AuditLogResponse,
)

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


def audit_log_data(audit_log: AuditLog) -> AuditLogData:
    """
    转换审计日志响应数据

    :param audit_log (AuditLog): 审计日志实体

    :return AuditLogData: 审计日志响应数据
    """

    return AuditLogData(
        id=audit_log.id,
        actor_user_id=audit_log.actor_user_id,
        action=audit_log.action,
        target_type=audit_log.target_type,
        target_id=audit_log.target_id,
        details=audit_log.details,
        created_at=audit_log.created_at,
    )


@router.get("", response_model=AuditLogListResponse)
async def list_audit_logs(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AuditLogListResponse:
    """
    列出可见审计日志

    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param limit (int): 最大返回数量

    :return AuditLogListResponse: 审计日志列表响应
    """

    actor_user_id = None if user.role == "admin" else user.id
    logs = await IdentityRepository(session).list_audit_logs(
        actor_user_id=actor_user_id,
        limit=limit,
    )
    return AuditLogListResponse(
        data=AuditLogListData(items=[audit_log_data(item) for item in logs]),
        request_id=get_request_id(),
    )


@router.get("/{audit_log_id}", response_model=AuditLogResponse)
async def get_audit_log(
    audit_log_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> AuditLogResponse:
    """
    读取可见审计日志

    :param audit_log_id (UUID): 审计日志 ID
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return AuditLogResponse: 审计日志响应
    """

    audit_log = await IdentityRepository(session).get_audit_log(audit_log_id)
    if audit_log is None:
        raise ApiError(code="COMMON_NOT_FOUND", message="Audit log was not found.", status_code=404)
    if user.role != "admin" and audit_log.actor_user_id != user.id:
        raise ApiError(code="COMMON_NOT_FOUND", message="Audit log was not found.", status_code=404)
    return AuditLogResponse(data=audit_log_data(audit_log), request_id=get_request_id())
