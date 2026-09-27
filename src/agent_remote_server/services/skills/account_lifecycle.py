"""
在既有账户删除条件之后清理逐字段规则，不扩大账户删除权限。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation


async def forget_account_overrides(session: AsyncSession, user_id: UUID, account_id: UUID) -> None:
    """
    与账户删除使用同一事务，只释放该账户的规则并推进库代数。

    :param session (AsyncSession): 已验证账户删除条件的外层事务
    :param user_id (UUID): 已授权用户标识
    :param account_id (UUID): 符合既有删除条件的账户标识
    """
    async with retention_mutation(session, user_id):
        repository = SkillLibraryRepository(session)
        library = await repository.lock_library(user_id)
        if await SkillRuntimeRepository(session).directory(user_id, account_id) is not None:
            raise SkillContentError("STATE_RETAINED", "account still owns retained skill state")
        if await repository.delete_account_overrides(user_id, account_id):
            if library.generation == 2**63 - 1:
                raise SkillContentError("GENERATION_EXHAUSTED", "library generation is exhausted")
            library.generation += 1
        await repository.flush()
