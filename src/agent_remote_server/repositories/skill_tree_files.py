"""
从完整树的原子对象引用授权单文件，不重复载入目录清单。
"""

from uuid import UUID

from sqlalchemy import literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillStoredTree,
    SkillTreeObjectReference,
)


class SkillTreeFileRepository:
    """
    调用方持有用户锁，完整树引用与跨分类删除标记在同一事务可见。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定当前已授权事务，不缓存内容或权限。

        :param session (AsyncSession): 当前请求的异步事务
        """
        self._session = session

    async def exists(self, user_id: UUID, category: str, tree_digest: str) -> bool:
        """
        只读取原树身份，不请求完整 JSON 列。

        :param user_id (UUID): 已认证所有者
        :param category (str): 原始内容类别
        :param tree_digest (str): 已由调用方授权的树摘要
        :return bool: 是否存在完全匹配的完整树
        """
        return (
            await self._session.scalar(
                select(SkillStoredTree.digest).where(
                    SkillStoredTree.user_id == user_id,
                    SkillStoredTree.category == category,
                    SkillStoredTree.digest == tree_digest,
                )
            )
            is not None
        )

    async def has_unavailable_member(self, user_id: UUID, category: str, tree_digest: str) -> bool:
        """
        从少量删除标记查询整树成员，任一分类的同用户共享字节都能阻断读取。

        :param user_id (UUID): 已锁定用户
        :param category (str): 授权树的类别，不限制删除标记的类别
        :param tree_digest (str): 本次原始完整树
        :return bool: 是否存在属于该树的不可用共享对象
        """
        membership = (
            select(SkillTreeObjectReference.object_digest)
            .where(
                SkillTreeObjectReference.user_id == user_id,
                SkillTreeObjectReference.category == category,
                SkillTreeObjectReference.tree_digest == tree_digest,
                SkillTreeObjectReference.object_digest == SkillContentObject.digest,
            )
            .correlate(SkillContentObject)
            .exists()
        )
        # 固定协议状态使用 SQL 常量，使 PostgreSQL 通用预备计划也可证明部分索引谓词。
        return (
            await self._session.scalar(
                select(SkillContentObject.digest)
                .where(
                    SkillContentObject.user_id == user_id,
                    SkillContentObject.status != literal_column("'available'"),
                    membership,
                )
                .limit(1)
            )
            is not None
        )

    async def member(
        self, user_id: UUID, category: str, tree_digest: str, file_digest: str
    ) -> SkillContentObject | None:
        """
        文件授权必须经过同用户、同类别、同树的精确引用，不能只查询对象摘要。

        :param user_id (UUID): 原始所有者
        :param category (str): 授权内容类别
        :param tree_digest (str): 授权完整树
        :param file_digest (str): 请求文件摘要
        :return SkillContentObject | None: 已验证对象元数据或不存在
        """
        return await self._session.scalar(
            select(SkillContentObject)
            .join(
                SkillTreeObjectReference,
                (SkillTreeObjectReference.user_id == SkillContentObject.user_id)
                & (SkillTreeObjectReference.category == SkillContentObject.category)
                & (SkillTreeObjectReference.object_digest == SkillContentObject.digest),
            )
            .where(
                SkillTreeObjectReference.user_id == user_id,
                SkillTreeObjectReference.category == category,
                SkillTreeObjectReference.tree_digest == tree_digest,
                SkillTreeObjectReference.object_digest == file_digest,
            )
            .execution_options(populate_existing=True)
        )
