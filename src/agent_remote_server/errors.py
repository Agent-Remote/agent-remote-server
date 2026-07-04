from fastapi import Request
from fastapi.responses import JSONResponse

from agent_remote_server.context import get_request_id


class ApiError(Exception):
    """
    API 业务错误
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """
    将业务错误转换为协议错误响应

    :param request (Request): 当前请求
    :param exc (ApiError): 业务错误

    :return JSONResponse: 错误响应
    """

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
            "request_id": get_request_id() or request.headers.get("x-request-id"),
        },
    )
