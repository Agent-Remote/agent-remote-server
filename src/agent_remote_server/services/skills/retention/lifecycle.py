"""
将既有会话状态变更纳入历史时钟，不为未使用技能的用户建立存储记录。
"""

from collections.abc import AsyncIterator, Collection
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.retention.clocks import retention_mutation


@asynccontextmanager
async def existing_history_mutations(
    session: AsyncSession, user_ids: Collection[UUID]
) -> AsyncIterator[None]:
    """
    批量对账按所有者固定顺序加锁，时钟上下文必须在调用方提交之前结束。

    :param session (AsyncSession): 已授权生命周期事务
    :param user_ids (Collection[UUID]): 来自持久化会话的用户集合
    :return AsyncIterator[None]: 引用与时钟原子更新范围
    """
    storage = SkillStorageRepository(session)
    async with AsyncExitStack() as stack:
        for user_id in sorted(set(user_ids)):
            if await storage.lock_existing_usage(user_id) is not None:
                await stack.enter_async_context(retention_mutation(session, user_id))
        yield


@asynccontextmanager
async def session_history_mutation(
    session: AsyncSession, session_id: UUID | None
) -> AsyncIterator[None]:
    """
    在任务回报写入之前取得实际会话所属用户锁，不更改原任务授权与提交协议。

    :param session (AsyncSession): 节点回报事务
    :param session_id (UUID | None): 原业务逻辑解析的目标会话
    :return AsyncIterator[None]: 会话与相关历史的共同变更范围
    """
    user_id = (
        await SkillRetentionRepository(session).session_owner(session_id)
        if session_id is not None
        else None
    )
    async with existing_history_mutations(session, () if user_id is None else (user_id,)):
        yield
