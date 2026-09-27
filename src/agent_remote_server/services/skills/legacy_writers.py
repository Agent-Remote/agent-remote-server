"""
在接管的同一用户锁内阻止新绑定和后端迁移重新写入旧账户目录。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.runtime_migrations import require_runtime_migration_settled


async def require_legacy_account_writer(
    session: AsyncSession, user_id: UUID, account_id: UUID
) -> None:
    """
    在任何账户或任务变更前取得内容锁，目录模式不受功能开关回退影响。

    :param session (AsyncSession): 持锁至提交的外层请求事务
    :param user_id (UUID): 已授权账户所有者
    :param account_id (UUID): 已授权账户身份
    """
    await SkillStorageRepository(session).lock_usage(user_id)
    directory = await SkillRuntimeRepository(session).directory(user_id, account_id)
    if directory is not None and directory.mode != "legacy":
        raise ApiError(
            code="MIGRATION_PENDING",
            message="Account skill migration prevents legacy binding or backend changes.",
            status_code=409,
        )
    await require_runtime_migration_settled(session, user_id, account_id)
