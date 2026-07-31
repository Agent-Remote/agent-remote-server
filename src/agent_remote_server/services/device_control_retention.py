from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.repositories.device_sessions import DeviceSessionRepository
from agent_remote_server.repositories.identity import IdentityRepository


@dataclass(frozen=True)
class DeviceControlRetentionResult:
    """
    设备控制元数据保留清理结果
    """

    sessions_deleted: int
    audits_deleted: int

    @property
    def changed(self) -> int:
        """
        返回本次删除的元数据总数

        :return int: 已删除元数据总数
        """

        return self.sessions_deleted + self.audits_deleted


class DeviceControlRetentionService:
    """
    设备控制元数据保留服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        初始化设备控制元数据保留服务

        :param session (AsyncSession): 异步数据库会话
        :param settings (Settings): 应用配置
        """

        self._session = session
        self._settings = settings
        self._device_sessions = DeviceSessionRepository(session)
        self._identity = IdentityRepository(session)

    async def cleanup(
        self,
        *,
        now: datetime | None = None,
    ) -> DeviceControlRetentionResult:
        """
        按部署方配置清理保留期外的设备控制元数据

        :param now (datetime | None): 可选清理基准时间

        :return DeviceControlRetentionResult: 本次清理结果
        """

        current_time = now or datetime.now(UTC)
        limit = self._settings.device_control_retention_cleanup_batch_size
        sessions_deleted = 0
        audits_deleted = 0
        if self._settings.device_session_retention_days > 0:
            sessions_deleted = await self._device_sessions.delete_terminal_before(
                current_time - timedelta(days=self._settings.device_session_retention_days),
                limit=limit,
            )
        if self._settings.device_session_audit_retention_days > 0:
            audits_deleted = await self._identity.delete_device_session_audits_before(
                current_time - timedelta(days=self._settings.device_session_audit_retention_days),
                limit=limit,
            )
        await self._session.commit()
        return DeviceControlRetentionResult(
            sessions_deleted=sessions_deleted,
            audits_deleted=audits_deleted,
        )
