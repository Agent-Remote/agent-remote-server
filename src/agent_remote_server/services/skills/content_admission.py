"""
在同一用户事务内验证共享文件可用性，不把计量分类当成独立物理文件。
"""

from uuid import UUID

from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.content_errors import SkillContentError


async def require_available_files(
    repository: SkillStorageRepository, user_id: UUID, digests: set[str]
) -> None:
    """
    所有调用方已持有用户锁，任一分类删除标记都使新内容引用整体失败。

    :param repository (SkillStorageRepository): 当前异步存储仓储
    :param user_id (UUID): 已认证且锁定的所有者
    :param digests (set[str]): 本次精确内容文件摘要集合
    """
    if await repository.has_unavailable_objects(user_id, digests):
        raise SkillContentError("CONTENT_UNAVAILABLE", "shared content is being retired")
