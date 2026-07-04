from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_remote_server import __version__
from agent_remote_server.api.health import router as health_router
from agent_remote_server.api.routes import api_router
from agent_remote_server.config import Settings, get_settings
from agent_remote_server.db import create_engine, create_session_factory
from agent_remote_server.errors import ApiError, api_error_handler
from agent_remote_server.logging import configure_logging
from agent_remote_server.middleware.request_id import RequestIdMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    创建 FastAPI 应用

    :param settings (Settings): 可选应用配置

    :return FastAPI: FastAPI 应用实例
    """

    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def _lifespan(current_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await current_app.state.database_engine.dispose()

    app = FastAPI(
        title="agent-remote-server",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )
    app.state.settings = app_settings
    app.state.database_engine = create_engine(app_settings)
    app.state.session_factory = create_session_factory(app_settings, app.state.database_engine)

    app.add_middleware(
        RequestIdMiddleware,
        header_name=app_settings.request_id_header,
    )

    async def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
        api_error = (
            exc
            if isinstance(exc, ApiError)
            else ApiError(
                code="COMMON_INTERNAL_ERROR",
                message="Unexpected server error.",
                status_code=500,
            )
        )
        return await api_error_handler(request, api_error)

    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(health_router)
    app.include_router(api_router)

    return app


app = create_app()
