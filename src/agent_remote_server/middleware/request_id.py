import logging
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from agent_remote_server.context import request_id_var

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    请求 ID 中间件

    为每个请求生成或透传 request_id，并把它写入响应头和日志上下文
    """

    def __init__(self, app: ASGIApp, *, header_name: str = "x-request-id") -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        处理请求 ID 注入和请求完成日志

        :param request (Request): 当前请求对象
        :param call_next (RequestResponseEndpoint): 下一个请求处理器

        :return Response: 响应对象
        """

        request_id = request.headers.get(self.header_name) or f"req_{uuid4().hex}"
        token = request_id_var.set(request_id)
        started_at = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
                },
            )
            raise
        finally:
            request_id_var.reset(token)

        response.headers[self.header_name] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            },
        )
        return response
