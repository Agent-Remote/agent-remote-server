from fastapi import FastAPI

from agent_remote_server import __version__
from agent_remote_server.api.health import router as health_router
from agent_remote_server.api.routes import api_router
from agent_remote_server.config import Settings, get_settings
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

    app = FastAPI(
        title="agent-remote-server",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = app_settings

    app.add_middleware(
        RequestIdMiddleware,
        header_name=app_settings.request_id_header,
    )
    app.include_router(health_router)
    app.include_router(api_router)

    return app


app = create_app()
