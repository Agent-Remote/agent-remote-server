import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from agent_remote_server import __version__
from agent_remote_server.api.health import router as health_router
from agent_remote_server.api.routes import api_router
from agent_remote_server.config import Settings, get_settings
from agent_remote_server.db import create_engine, create_session_factory
from agent_remote_server.device_control_release import (
    ensure_device_control_release_evidence_current,
    verify_device_control_release_evidence,
)
from agent_remote_server.device_control_retention import run_device_control_retention
from agent_remote_server.device_relay_hub import DeviceRelayHub
from agent_remote_server.device_relay_revocation import create_device_relay_revocation_bus
from agent_remote_server.device_relay_store import create_device_relay_store
from agent_remote_server.errors import ApiError, api_error_handler
from agent_remote_server.logging import configure_logging
from agent_remote_server.middleware.request_id import RequestIdMiddleware
from agent_remote_server.port_forward_cleanup import run_port_forward_cleanup
from agent_remote_server.port_forward_tokens import create_port_forward_token_store


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    创建 FastAPI 应用

    :param settings (Settings): 可选应用配置

    :return FastAPI: FastAPI 应用实例

    :raises DeviceControlReleaseEvidenceError: 生产设备控制发布证据未通过校验
    """

    app_settings = settings or get_settings()
    device_control_release_evidence = None
    if (
        app_settings.environment.strip().lower() == "production"
        and app_settings.device_control_enabled
    ):
        device_control_release_evidence = verify_device_control_release_evidence(
            evidence_path=app_settings.device_control_release_evidence_path,
            public_key_base64=app_settings.device_control_release_public_key,
        )
        ensure_device_control_release_evidence_current(
            environment=app_settings.environment,
            enabled=app_settings.device_control_enabled,
            evidence=device_control_release_evidence,
        )
    configure_logging(app_settings.log_level)

    @asynccontextmanager
    async def _lifespan(current_app: FastAPI) -> AsyncIterator[None]:
        cleanup_stop = asyncio.Event()
        port_forward_cleanup_task = asyncio.create_task(
            run_port_forward_cleanup(current_app, cleanup_stop)
        )
        device_control_retention_task = asyncio.create_task(
            run_device_control_retention(current_app, cleanup_stop)
        )
        await current_app.state.device_relay_revocation_bus.start(
            current_app.state.device_relay_hub.close_binding_remote
        )
        try:
            yield
        finally:
            cleanup_stop.set()
            await asyncio.gather(port_forward_cleanup_task, device_control_retention_task)
            await current_app.state.device_relay_store.close()
            await current_app.state.device_relay_revocation_bus.close()
            await current_app.state.port_forward_token_store.close()
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
    app.state.device_control_release_evidence = device_control_release_evidence
    app.state.database_engine = create_engine(app_settings)
    app.state.session_factory = create_session_factory(app_settings, app.state.database_engine)
    app.state.port_forward_token_store = create_port_forward_token_store(app_settings)
    app.state.device_relay_store = create_device_relay_store(app_settings)
    app.state.device_relay_revocation_bus = create_device_relay_revocation_bus(app_settings)
    app.state.device_relay_hub = DeviceRelayHub(
        maximum_frame_bytes=app_settings.device_relay_max_frame_bytes,
        pair_timeout_seconds=app_settings.device_relay_pair_timeout_seconds,
        maximum_bytes_per_second=app_settings.device_relay_max_bytes_per_second,
        maximum_connection_seconds=app_settings.device_relay_max_connection_seconds,
        revocation_bus=app.state.device_relay_revocation_bus,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
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
