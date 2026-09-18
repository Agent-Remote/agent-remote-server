"""
收敛 ego-browser binding 租约和撤销 outbox。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.nodes import NodeService

logger = logging.getLogger(__name__)


async def run_ego_browser_cleanup(app: FastAPI, stop: asyncio.Event) -> None:
    """
    周期性过期 binding 并发布已提交的撤销事件。

    :param app (FastAPI): FastAPI 应用实例
    :param stop (asyncio.Event): 后台清理任务的停止信号
    """

    settings = app.state.settings
    interval = settings.ego_browser_cleanup_interval_seconds
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            async with app.state.session_factory() as session:
                stale_nodes = await NodeService(
                    session,
                    settings,
                    ego_browser_revocation_publisher=app.state.ego_browser_revocation_bus,
                ).expire_stale_nodes()
                service = EgoBrowserService(
                    session,
                    settings,
                    revocation_publisher=app.state.ego_browser_revocation_bus,
                )
                expired = await service.expire_due()
                # 兼容尚未实现保留期清理的旧 worker，避免混合版本阻塞生命周期循环。
                expire_ensure_results = getattr(service, "expire_ensure_results", None)
                if expire_ensure_results is None:
                    expired_ensure_results = 0
                else:
                    expired_ensure_results = await expire_ensure_results(
                        limit=settings.ego_browser_cleanup_batch_size
                    )
                published = await service.publish_pending_revocations(
                    limit=settings.ego_browser_cleanup_batch_size
                )
            if stale_nodes or expired or expired_ensure_results or published:
                logger.info(
                    "ego browser lifecycle cleanup completed",
                    extra={
                        "stale_nodes": stale_nodes,
                        "expired": expired,
                        "expired_ensure_results": expired_ensure_results,
                        "revocations_published": published,
                    },
                )
        except Exception:
            logger.exception("ego browser lifecycle cleanup failed")
