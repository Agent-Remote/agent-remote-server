import asyncio
import logging

from fastapi import FastAPI

from agent_remote_server.services.device_control_retention import DeviceControlRetentionService

logger = logging.getLogger(__name__)


async def run_device_control_retention(app: FastAPI, stop: asyncio.Event) -> None:
    """
    周期性清理保留期外的设备控制元数据

    :param app (FastAPI): 当前应用
    :param stop (asyncio.Event): 停止信号
    """

    settings = app.state.settings
    if (
        settings.device_session_retention_days == 0
        and settings.device_session_audit_retention_days == 0
    ):
        await stop.wait()
        return
    interval = settings.device_control_retention_cleanup_interval_seconds
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            async with app.state.session_factory() as session:
                result = await DeviceControlRetentionService(session, settings).cleanup()
            if result.changed:
                logger.info(
                    "device control metadata retention cleanup completed",
                    extra={
                        "sessions_deleted": result.sessions_deleted,
                        "audits_deleted": result.audits_deleted,
                    },
                )
        except Exception:
            logger.exception("device control metadata retention cleanup failed")
