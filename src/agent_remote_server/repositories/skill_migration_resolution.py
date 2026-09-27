"""
在精确迁移归属内保存人工内容授权、计划版本和幂等回执。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionChoice,
    SkillMigrationResolutionContent,
    SkillMigrationResolutionOperation,
    SkillMigrationResolutionPlan,
    SkillMigrationResolutionUpload,
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_storage import SkillContentUpload


class SkillMigrationResolutionRepository:
    """
    上层写操作持有用户内容锁，读取不创建计划或授权。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        共享外层用户事务。

        :param session (AsyncSession): 异步请求事务
        """
        self._session = session

    async def upload(
        self, migration: SkillBranchPreparation, upload_id: UUID
    ) -> SkillContentUpload | None:
        """
        使用实际绑定行授权租约，伪造保留键前缀不能取得迁移权限。

        :param migration (SkillBranchPreparation): 已授权迁移
        :param upload_id (UUID): 原始上传租约身份
        :return SkillContentUpload | None: 精确范围匹配的租约
        """
        return await self._session.scalar(
            select(SkillContentUpload)
            .join(
                SkillMigrationResolutionUpload,
                SkillMigrationResolutionUpload.upload_id == SkillContentUpload.id,
            )
            .where(
                SkillMigrationResolutionUpload.user_id == migration.user_id,
                SkillMigrationResolutionUpload.account_id == migration.account_id,
                SkillMigrationResolutionUpload.migration_id == migration.id,
                SkillMigrationResolutionUpload.upload_id == upload_id,
                SkillContentUpload.user_id == migration.user_id,
            )
            .execution_options(populate_existing=True)
        )

    async def bind_upload(
        self, migration: SkillBranchPreparation, upload: SkillContentUpload
    ) -> None:
        """
        新受理和迁移归属同事务保存，重复同键保持原绑定。

        :param migration (SkillBranchPreparation): 已授权迁移
        :param upload (SkillContentUpload): 本次受理租约
        """
        if await self.upload(migration, upload.id) is None:
            self._session.add(
                SkillMigrationResolutionUpload(
                    upload_id=upload.id,
                    user_id=migration.user_id,
                    account_id=migration.account_id,
                    migration_id=migration.id,
                    tree_digest=upload.tree_digest,
                    scope=upload.scope,
                )
            )
            await self._session.flush()

    async def upload_by_key(
        self, migration: SkillBranchPreparation, key: str
    ) -> SkillContentUpload | None:
        """
        原响应丢失后用持久化键恢复真实绑定，不接受仅有相同前缀的普通上传。

        :param migration (SkillBranchPreparation): 已授权迁移
        :param key (str): 由服务构造的完整上传键
        :return SkillContentUpload | None: 本迁移已绑定的原租约
        """
        return await self._session.scalar(
            select(SkillContentUpload)
            .join(
                SkillMigrationResolutionUpload,
                SkillMigrationResolutionUpload.upload_id == SkillContentUpload.id,
            )
            .where(
                SkillMigrationResolutionUpload.user_id == migration.user_id,
                SkillMigrationResolutionUpload.account_id == migration.account_id,
                SkillMigrationResolutionUpload.migration_id == migration.id,
                SkillContentUpload.user_id == migration.user_id,
                SkillContentUpload.idempotency_key == key,
            )
            .execution_options(populate_existing=True)
        )

    async def plan(self, migration: SkillBranchPreparation) -> SkillMigrationResolutionPlan | None:
        """
        只在保存迁移的相同所有者和账户内查找计划。

        :param migration (SkillBranchPreparation): 已授权迁移
        :return SkillMigrationResolutionPlan | None: 已有计划或不存在
        """
        return await self._session.scalar(
            select(SkillMigrationResolutionPlan)
            .where(
                SkillMigrationResolutionPlan.user_id == migration.user_id,
                SkillMigrationResolutionPlan.account_id == migration.account_id,
                SkillMigrationResolutionPlan.migration_id == migration.id,
            )
            .execution_options(populate_existing=True)
        )

    async def choices(
        self, migration: SkillBranchPreparation
    ) -> Sequence[SkillMigrationResolutionChoice]:
        """
        读取本迁移独立保活的选择，不从其他计划复制选择。

        :param migration (SkillBranchPreparation): 已授权迁移
        :return Sequence[SkillMigrationResolutionChoice]: 稳定范围顺序的选择
        """
        return (
            await self._session.scalars(
                select(SkillMigrationResolutionChoice)
                .where(
                    SkillMigrationResolutionChoice.user_id == migration.user_id,
                    SkillMigrationResolutionChoice.account_id == migration.account_id,
                    SkillMigrationResolutionChoice.migration_id == migration.id,
                )
                .order_by(SkillMigrationResolutionChoice.selector_key)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def content(
        self, migration: SkillBranchPreparation, digest: str
    ) -> SkillMigrationResolutionContent | None:
        """
        相同用户的其他树或其他迁移授权不能替用。

        :param migration (SkillBranchPreparation): 已授权迁移
        :param digest (str): 待引用状态树摘要
        :return SkillMigrationResolutionContent | None: 精确迁移范围的完成授权
        """
        return await self._session.scalar(
            select(SkillMigrationResolutionContent).where(
                SkillMigrationResolutionContent.user_id == migration.user_id,
                SkillMigrationResolutionContent.account_id == migration.account_id,
                SkillMigrationResolutionContent.migration_id == migration.id,
                SkillMigrationResolutionContent.category == "state",
                SkillMigrationResolutionContent.tree_digest == digest,
            )
        )

    async def retain_content(self, migration: SkillBranchPreparation, digest: str) -> None:
        """
        上层已完整验证字节，重复完成相同树只保留一份明确授权。

        :param migration (SkillBranchPreparation): 已授权原始迁移
        :param digest (str): 完整已提交状态树摘要
        """
        if await self.content(migration, digest) is None:
            self._session.add(
                SkillMigrationResolutionContent(
                    user_id=migration.user_id,
                    account_id=migration.account_id,
                    migration_id=migration.id,
                    tree_digest=digest,
                )
            )
            await self._session.flush()

    async def replace_choices(
        self,
        migration: SkillBranchPreparation,
        expected_revision: int,
        choices: Sequence[SkillMigrationResolutionChoice],
    ) -> bool:
        """
        计划版本交换和整组选择替换处于保存点，晚到失败不能留下半个计划。

        :param migration (SkillBranchPreparation): 已授权迁移
        :param expected_revision (int): 上层已检查的旧计划版本
        :param choices (Sequence[SkillMigrationResolutionChoice]): 已验证非重叠完整选择
        :return bool: 是否成功替换，旧版本不匹配返回假
        """
        if not 0 <= expected_revision < 2**63 - 1:
            return False
        if any(
            (choice.user_id, choice.account_id, choice.migration_id)
            != (migration.user_id, migration.account_id, migration.id)
            for choice in choices
        ):
            raise ValueError("choice does not belong to this migration")
        async with self._session.begin_nested():
            plan = await self.plan(migration)
            if plan is None:
                if expected_revision != 0:
                    return False
                self._session.add(
                    SkillMigrationResolutionPlan(
                        user_id=migration.user_id,
                        account_id=migration.account_id,
                        migration_id=migration.id,
                        revision=1,
                    )
                )
                await self._session.flush()
            else:
                changed = await self._session.scalar(
                    update(SkillMigrationResolutionPlan)
                    .where(
                        SkillMigrationResolutionPlan.user_id == migration.user_id,
                        SkillMigrationResolutionPlan.account_id == migration.account_id,
                        SkillMigrationResolutionPlan.migration_id == migration.id,
                        SkillMigrationResolutionPlan.revision == expected_revision,
                    )
                    .values(revision=expected_revision + 1)
                    .returning(SkillMigrationResolutionPlan.revision)
                )
                if changed is None:
                    return False
            await self._session.execute(
                delete(SkillMigrationResolutionChoice).where(
                    SkillMigrationResolutionChoice.user_id == migration.user_id,
                    SkillMigrationResolutionChoice.account_id == migration.account_id,
                    SkillMigrationResolutionChoice.migration_id == migration.id,
                )
            )
            self._session.add_all(choices)
            await self._session.flush()
        return True

    async def operation_by_id(
        self, user_id: UUID, operation_id: UUID
    ) -> SkillMigrationResolutionOperation | None:
        """
        按原受理身份和认证用户查询，不执行任何解决选择。

        :param user_id (UUID): 当前认证所有者
        :param operation_id (UUID): 原始受理身份
        :return SkillMigrationResolutionOperation | None: 原始回执或不存在
        """
        return await self._session.scalar(
            select(SkillMigrationResolutionOperation).where(
                SkillMigrationResolutionOperation.user_id == user_id,
                SkillMigrationResolutionOperation.id == operation_id,
            )
        )

    async def operation(self, user_id: UUID, key: str) -> SkillMigrationResolutionOperation | None:
        """
        原始幂等结果仅按用户键恢复，不重新执行旧选择。

        :param user_id (UUID): 已认证用户
        :param key (str): 持久化请求键
        :return SkillMigrationResolutionOperation | None: 原始操作或不存在
        """
        return await self._session.scalar(
            select(SkillMigrationResolutionOperation).where(
                SkillMigrationResolutionOperation.user_id == user_id,
                SkillMigrationResolutionOperation.idempotency_key == key,
            )
        )

    async def save_operation(self, operation: SkillMigrationResolutionOperation) -> None:
        """
        原响应和选择交换必须共享调用方保存点，唯一键失败不得单独提交计划。

        :param operation (SkillMigrationResolutionOperation): 已授权完整不可变回执
        """
        self._session.add(operation)
        await self._session.flush()
