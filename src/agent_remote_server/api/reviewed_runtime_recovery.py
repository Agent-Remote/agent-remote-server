"""
新增仅用户管理员使用的迁移安全观察和强确认入口。
"""

from collections.abc import Callable, Coroutine
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_user_token_readonly,
    get_session,
    require_user_admin_readonly,
)
from agent_remote_server.context import get_request_id
from agent_remote_server.errors import ApiError
from agent_remote_server.models import User
from agent_remote_server.schemas.reviewed_runtime_recovery import (
    MigrationTaskId,
    RecoveryAuthorityData,
    RecoveryAuthorityResponse,
    RecoveryInspectionResponse,
    RecoveryKey,
    ReviewedRecoveryRequest,
    ReviewedRecoveryResponse,
)
from agent_remote_server.services.reviewed_runtime_recovery import ReviewedRuntimeRecoveryService


class SafeRecoveryRoute(APIRoute):
    """
    在解析前限制请求大小，并禁止校验错误回显任意输入。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        """
        包装新路由，不改变旧管理员入口。

        :return Callable[[Request], Coroutine[None, None, Response]]: 有界安全处理器
        """
        original = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            """
            限制完整分块请求并返回固定校验错误。

            :param request (Request): 当前原始请求
            :return Response: 安全路由响应
            """
            try:
                if request.method == "POST":
                    buffer = bytearray()
                    async for chunk in request.stream():
                        if len(buffer) + len(chunk) > 8192:
                            raise ApiError(
                                code="RUNTIME_RECOVERY_REQUEST_TOO_LARGE",
                                message="Recovery request is too large.",
                                status_code=413,
                            )
                        buffer.extend(chunk)
                    request._body = bytes(buffer)
                return await original(request)
            except RequestValidationError:
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message="Invalid recovery request.",
                    status_code=422,
                ) from None

        return guarded


router = APIRouter(route_class=SafeRecoveryRoute, tags=["runtime-recovery"])


@router.get("/runtime-recovery/authority", response_model=RecoveryAuthorityResponse)
async def authority(
    user: Annotated[User, Depends(get_current_user_token_readonly)],
) -> RecoveryAuthorityResponse:
    """
    普通用户也能只读观察自己的管理员标志。

    :param user (User): 当前独立用户凭据身份
    :return RecoveryAuthorityResponse: 当前权限观察
    """
    return RecoveryAuthorityResponse(
        data=RecoveryAuthorityData(
            schema_version=1, actor_user_id=str(user.id), admin=user.role == "admin"
        ),
        request_id=get_request_id(),
    )


@router.get(
    "/tool-accounts/{tool_account_id}/runtime-migration/recovery-inspection",
    response_model=RecoveryInspectionResponse,
)
async def inspect_original(
    tool_account_id: UUID,
    original_task_id: MigrationTaskId,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_user_admin_readonly)],
) -> RecoveryInspectionResponse:
    """
    观察显式原任务，不将观察等同执行授权。

    :param tool_account_id (UUID): 选择的账户
    :param original_task_id (MigrationTaskId): 显式原迁移任务
    :param session (AsyncSession): 请求事务
    :param admin (User): 当前用户管理员
    :return RecoveryInspectionResponse: 安全原任务观察
    """
    data = await ReviewedRuntimeRecoveryService(session).inspect(tool_account_id, original_task_id)
    return RecoveryInspectionResponse(data=data, request_id=get_request_id())


@router.post(
    "/tool-accounts/{tool_account_id}/runtime-migration/recovery-requests",
    response_model=ReviewedRecoveryResponse,
)
async def submit_request(
    tool_account_id: UUID,
    payload: ReviewedRecoveryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_user_admin_readonly)],
) -> ReviewedRecoveryResponse:
    """
    在既有恢复事务内核对确认投影并受理。

    :param tool_account_id (UUID): 选择的账户
    :param payload (ReviewedRecoveryRequest): 完整确认请求
    :param session (AsyncSession): 请求事务
    :param admin (User): 当前用户管理员
    :return ReviewedRecoveryResponse: 原任务受理或重放结果
    """
    data = await ReviewedRuntimeRecoveryService(session).submit(admin, tool_account_id, payload)
    await session.commit()
    return ReviewedRecoveryResponse(data=data, request_id=get_request_id())


@router.get(
    "/tool-accounts/{tool_account_id}/runtime-migration/recovery-requests/{request_id}",
    response_model=ReviewedRecoveryResponse,
)
async def request_status(
    tool_account_id: UUID,
    request_id: RecoveryKey,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_user_admin_readonly)],
) -> ReviewedRecoveryResponse:
    """
    只读恢复原始键，不派发新任务或跟随最新迁移。

    :param tool_account_id (UUID): 原账户
    :param request_id (RecoveryKey): 规范非零原键
    :param session (AsyncSession): 请求事务
    :param admin (User): 当前用户管理员
    :return ReviewedRecoveryResponse: 原恢复任务当前状态
    """
    data = await ReviewedRuntimeRecoveryService(session).status(tool_account_id, request_id)
    return ReviewedRecoveryResponse(data=data, request_id=get_request_id())
