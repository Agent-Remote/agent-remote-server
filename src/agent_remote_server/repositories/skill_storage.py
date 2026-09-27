"""
封装用户内容、上传租约和配额事务的数据库访问。
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, undefer

from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
    SkillStoredTree,
    SkillTreeObjectReference,
)

type SkillStorageRecord = (
    SkillContentUpload | SkillContentObject | SkillStoredTree | SkillTreeObjectReference
)


class SkillStorageRepository:
    """
    所有读写均显式带用户范围的内容仓储。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定调用方负责提交的事务。

        :param session (AsyncSession): 异步数据库会话
        """
        self._session = session

    async def lock_usage(self, user_id: UUID) -> SkillStorageUsage:
        """
        原子建立用户计量行并通过写锁串行化配额和引用变更。

        :param user_id (UUID): 已授权用户标识
        :return SkillStorageUsage: 已锁定的最新计量状态
        """
        dialect = self._session.get_bind().dialect.name
        if dialect == "postgresql":
            statement: PostgresInsert | SqliteInsert = PostgresInsert(SkillStorageUsage)
        elif dialect == "sqlite":
            statement = SqliteInsert(SkillStorageUsage)
        else:
            raise ValueError("unsupported skill storage database")
        await self._session.execute(statement.values(user_id=user_id).on_conflict_do_nothing())
        await self._session.execute(
            update(SkillStorageUsage)
            .where(SkillStorageUsage.user_id == user_id)
            .values(lock_version=SkillStorageUsage.lock_version + 1)
        )
        result = await self._session.scalar(
            select(SkillStorageUsage)
            .where(SkillStorageUsage.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        assert result is not None
        return result

    async def lock_existing_usage(self, user_id: UUID) -> SkillStorageUsage | None:
        """
        查询只锁定已有用户行，不为只读或 dry-run 创建持久化记录。

        :param user_id (UUID): 认证用户标识
        :return SkillStorageUsage | None: 已有计量行，未使用存储时为空
        """
        return await self._session.scalar(
            select(SkillStorageUsage)
            .where(SkillStorageUsage.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def lock_existing_usage_for_mutation(self, user_id: UUID) -> bool:
        """
        在保存点之前锁住既有行；空更新也建立 SQLite 外层写事务，但不创建失败请求记录。

        :param user_id (UUID): 当前写请求的所有者
        :return bool: 是否已取得既有用户锁，新用户须在保存点内创建行
        """
        identity = await self._session.scalar(
            update(SkillStorageUsage)
            .where(SkillStorageUsage.user_id == user_id)
            .values(lock_version=SkillStorageUsage.lock_version + 1)
            .returning(SkillStorageUsage.user_id)
        )
        return identity is not None

    async def upload_by_key(self, user_id: UUID, key: str) -> SkillContentUpload | None:
        """
        读取同用户原始幂等受理记录。

        :param user_id (UUID): 用户标识
        :param key (str): 客户端幂等键
        :return SkillContentUpload | None: 原始上传计划
        """
        return await self._session.scalar(
            select(SkillContentUpload).where(
                SkillContentUpload.user_id == user_id, SkillContentUpload.idempotency_key == key
            )
        )

    async def upload(
        self, user_id: UUID, upload_id: UUID, *, include_manifest: bool = True
    ) -> SkillContentUpload | None:
        """
        查询指定用户的上传记录。

        :param user_id (UUID): 用户标识
        :param upload_id (UUID): 上传标识
        :param include_manifest (bool): 是否读取完整清单，仅单文件检查时省略
        :return SkillContentUpload | None: 归属匹配的记录
        """
        return await self._session.scalar(
            select(SkillContentUpload)
            .options(
                undefer(SkillContentUpload.manifest_json)
                if include_manifest
                else defer(SkillContentUpload.manifest_json, raiseload=True)
            )
            .where(SkillContentUpload.user_id == user_id, SkillContentUpload.id == upload_id)
            .execution_options(populate_existing=True)
        )

    async def expired_uploads(self, user_id: UUID, now: datetime) -> Sequence[SkillContentUpload]:
        """
        读取尚未释放额度的过期上传。

        :param user_id (UUID): 用户标识
        :param now (datetime): 当前服务端时间
        :return Sequence[SkillContentUpload]: 需要终结的上传记录
        """
        return (
            await self._session.scalars(
                select(SkillContentUpload).where(
                    SkillContentUpload.user_id == user_id,
                    SkillContentUpload.status == "staged",
                    SkillContentUpload.expires_at <= now,
                )
            )
        ).all()

    async def retained_digests(self, user_id: UUID) -> set[str]:
        """
        读取各额度分类已登记的全部内容身份。

        :param user_id (UUID): 用户标识
        :return set[str]: 已登记的保活摘要
        """
        return set(
            await self._session.scalars(
                select(SkillContentObject.digest).where(SkillContentObject.user_id == user_id)
            )
        )

    async def active_uploads(self, user_id: UUID, now: datetime) -> Sequence[SkillContentUpload]:
        """
        取得仍有有效租约的上传计划。

        :param user_id (UUID): 用户标识
        :param now (datetime): 当前服务端时间
        :return Sequence[SkillContentUpload]: 仍可上传的计划
        """
        return (
            await self._session.scalars(
                select(SkillContentUpload).where(
                    SkillContentUpload.user_id == user_id,
                    SkillContentUpload.status == "staged",
                    SkillContentUpload.expires_at > now,
                )
            )
        ).all()

    async def objects(
        self, user_id: UUID, category: str, digests: set[str]
    ) -> dict[str, SkillContentObject]:
        """
        分批取得所需对象，避免大型清单超出数据库绑定参数上限。

        :param user_id (UUID): 用户标识
        :param category (str): 包或运行状态分类
        :param digests (set[str]): 本次需要的摘要集合
        :return dict[str, SkillContentObject]: 已登记对象索引
        """
        result: dict[str, SkillContentObject] = {}
        ordered = sorted(digests)
        for offset in range(0, len(ordered), 500):
            rows = await self._session.scalars(
                select(SkillContentObject).where(
                    SkillContentObject.user_id == user_id,
                    SkillContentObject.category == category,
                    SkillContentObject.digest.in_(ordered[offset : offset + 500]),
                )
            )
            result.update((row.digest, row) for row in rows)
        return result

    async def has_unavailable_objects(self, user_id: UUID, digests: set[str]) -> bool:
        """
        按真实共享文件身份检查所有计量分类，不让另一分类绕过删除标记复用相同字节。

        :param user_id (UUID): 已取得用户锁的所有者
        :param digests (set[str]): 本次将读取或引用的唯一文件摘要
        :return bool: 是否至少一个共享文件已被任一分类标记为不可用
        """
        ordered = sorted(digests)
        for offset in range(0, len(ordered), 500):
            if (
                await self._session.scalar(
                    select(SkillContentObject.digest)
                    .where(
                        SkillContentObject.user_id == user_id,
                        SkillContentObject.digest.in_(ordered[offset : offset + 500]),
                        SkillContentObject.status != "available",
                    )
                    .limit(1)
                )
                is not None
            ):
                return True
        return False

    async def tree(self, user_id: UUID, category: str, digest: str) -> SkillStoredTree | None:
        """
        只返回归属用户及指定额度分类的完整树。

        :param user_id (UUID): 用户标识
        :param category (str): 内容分类
        :param digest (str): 树摘要
        :return SkillStoredTree | None: 完整树引用
        """
        return await self._session.scalar(
            select(SkillStoredTree)
            .where(
                SkillStoredTree.user_id == user_id,
                SkillStoredTree.category == category,
                SkillStoredTree.digest == digest,
            )
            .execution_options(populate_existing=True)
        )

    def add(
        self,
        record: SkillStorageRecord,
    ) -> None:
        """
        加入同一事务，由服务完成业务验证后统一提交。

        :param record (SkillStorageRecord): 内容或上传记录
        """
        self._session.add(record)

    async def flush(self) -> None:
        """
        在返回结果前验证数据库约束，不自行提交调用方事务。
        """
        await self._session.flush()
