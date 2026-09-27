"""
应用生命周期只消费已提交的删除任务，不自行扩张树或历史清理范围。
"""

import asyncio
import logging

from fastapi import FastAPI

from agent_remote_server.config import Settings
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

logger = logging.getLogger(__name__)


async def run_skill_content_deletions(app: FastAPI, stop: asyncio.Event) -> None:
    """
    停用功能时不访问内容卷；启用后有界重试任务，关闭等待本轮磁盘操作真实退出。

    :param app (FastAPI): 应用与独立事务工厂
    :param stop (asyncio.Event): 应用关闭信号
    """
    settings: Settings = app.state.settings
    if not settings.skill_manager_enabled:
        await stop.wait()
        return
    worker = SkillContentDeletionWorker(
        app.state.session_factory, PrivateObjectStore(settings.skill_storage_root)
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.skill_deletion_interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            await worker.run_once(settings.skill_deletion_batch_size)
        except Exception as error:
            logger.warning(
                "skill content deletion round failed", extra={"error_type": type(error).__name__}
            )
