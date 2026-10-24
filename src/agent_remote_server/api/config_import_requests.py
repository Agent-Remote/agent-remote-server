"""
提供强版本导入受理和不修改任何状态的原始调用方键恢复 API。
"""

from collections.abc import Callable, Coroutine
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_user,
    get_current_user_readonly,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.errors import ApiError
from agent_remote_server.models import User
from agent_remote_server.schemas.config_import_requests import (
    CanonicalUuid,
    ConfigImportReceiptResponse,
    ConfigImportRequest,
)
from agent_remote_server.services.config_import_intent import invalid_import
from agent_remote_server.services.config_import_requests import ConfigImportRequestService

MAX_IMPORT_REQUEST_BYTES = 16 * 1024 * 1024


async def bounded_body(request: Request) -> None:
    """
    在 JSON 解析之前限制总请求流大小，不能只信任可缺失的长度头。

    :param request (Request): 尚未解析正文的原请求
    """
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > MAX_IMPORT_REQUEST_BYTES:
            raise ApiError(
                code="CONFIG_IMPORT_TOO_LARGE",
                message="Config import request is too large.",
                status_code=413,
            )
        buffer.extend(chunk)
    # Starlette 的正文缓存让原类型路由复用已限额的数据，不再次读取输入流。
    request._body = bytes(buffer)


class SafeImportRoute(APIRoute):
    """
    禁止默认校验错误把原配置正文或未知字段输入回显到响应。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        """
        包装本路由的解析错误，不改变其他兼容 API 的错误格式。

        :return Callable[[Request], Coroutine[None, None, Response]]: 不回显输入的路由处理器
        """
        original = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            """
            将请求模型错误转换为固定安全代码。

            :param request (Request): 当前 HTTP 请求
            :return Response: 通过严格模型校验的响应
            """
            try:
                if request.method == "POST":
                    await bounded_body(request)
                return await original(request)
            except RequestValidationError:
                raise invalid_import() from None

        return guarded


router = APIRouter(route_class=SafeImportRoute)


@router.post(
    "/{tool_account_id}/config-import-requests",
    response_model=ConfigImportReceiptResponse,
    status_code=201,
)
async def create_config_import_request(
    tool_account_id: UUID,
    payload: ConfigImportRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ConfigImportReceiptResponse:
    """
    仅受理完整已审核意图，相同键重放返回原始任务。

    :param tool_account_id (UUID): 目标账户身份
    :param payload (ConfigImportRequest): 严格版本请求
    :param response (Response): 用于区分首次接受和精确重放的响应
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 请求级事务
    :param user (User): 当前活动用户
    :return ConfigImportReceiptResponse: 原始不可变受理数据
    """
    receipt, created = await ConfigImportRequestService(session, settings).create(
        user, tool_account_id, payload
    )
    response.status_code = 201 if created else 200
    return ConfigImportReceiptResponse(data=receipt, request_id=get_request_id())


@router.get(
    "/{tool_account_id}/config-import-requests/{request_id}",
    response_model=ConfigImportReceiptResponse,
)
async def recover_config_import_request(
    tool_account_id: UUID,
    request_id: CanonicalUuid,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user_readonly)],
) -> ConfigImportReceiptResponse:
    """
    丢失响应后只查精确原始键，不创建审计、刷新活动或自动重派。

    :param tool_account_id (UUID): 原始账户身份
    :param request_id (CanonicalUuid): 规范原始请求身份
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 当前只读事务
    :param user (User): 经只读认证重新确认的活动用户
    :return ConfigImportReceiptResponse: 原始受理凭据
    """
    receipt = await ConfigImportRequestService(session, settings).get(
        user, tool_account_id, UUID(request_id)
    )
    return ConfigImportReceiptResponse(data=receipt, request_id=get_request_id())
