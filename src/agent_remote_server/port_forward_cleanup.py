import asyncio
import logging

from fastapi import FastAPI

from agent_remote_server.services.port_forwards import PortForwardService

logger = logging.getLogger(__name__)


async def run_port_forward_cleanup(app: FastAPI, stop: asyncio.Event) -> None:
    """
    周期性收敛端口转发生命周期

    :param app (FastAPI): 当前应用
    :param stop (asyncio.Event): 停止信号
    """

    interval = app.state.settings.port_forward_cleanup_interval_seconds
    cursor = None
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            async with app.state.session_factory() as session:
                result = await PortForwardService(
                    session,
                    app.state.settings,
                    app.state.port_forward_token_store,
                ).cleanup(after_id=cursor)
                cursor = result.next_cursor
            if result.changed:
                logger.info(
                    "port forward lifecycle cleanup completed",
                    extra={"changed": result.changed},
                )
        except Exception:
            logger.exception("port forward lifecycle cleanup failed")
