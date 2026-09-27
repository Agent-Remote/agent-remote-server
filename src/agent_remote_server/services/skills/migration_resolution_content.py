"""
受理迁移专用人工内容并保活精确授权，上传本身不发布或修改计划。
"""

import hashlib
from typing import BinaryIO
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStoredTree
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_migration_resolution import SkillMigrationResolutionPlanView
from agent_remote_server.schemas.skill_resolution import SkillResolutionUploadRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_resolution_choices import choice_spec
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillMigrationResolutionContentService:
    """
    真实绑定授权租约，完成授权引用与内容提交共享保存点。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享用户请求事务和对象存储。

        :param session (AsyncSession): 外层请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 存储策略
        """
        self._session = session
        self.conflicts = SkillMigrationConflictService(session, store, policy)
        self.repository = SkillMigrationResolutionRepository(session)
        self.content = self.conflicts.queries.content
        self._verification = MigrationContent(self.conflicts.queries, store)

    async def begin(
        self, user_id: UUID, migration_id: UUID, request: SkillResolutionUploadRequest
    ) -> SkillContentUpload:
        """
        活跃冲突下原子受理租约和绑定，失败不能留下无归属的新上传。

        :param user_id (UUID): 认证所有者
        :param migration_id (UUID): 原始迁移身份
        :param request (SkillResolutionUploadRequest): 完整清单及用户键
        :return SkillContentUpload: 可恢复的原租约
        """
        async with self._session.begin_nested():
            await self.conflicts.queries.library.lock_library(user_id)
            migration = await self.conflicts.require(user_id, migration_id)
            key = (
                f"migration-resolve:{migration_id}:upload:"
                + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
            )
            previous = await self.repository.upload_by_key(migration, key)
            if previous is not None:
                if previous.tree_digest != manifest_digest(request.manifest):
                    raise SkillContentError(
                        "IDEMPOTENCY_CONFLICT", "key belongs to another manifest"
                    )
                return await self.content.get(user_id, previous.id)
            if migration.status != "conflicted":
                raise SkillContentError(
                    "CONFLICT_NOT_ACTIVE", "migration cannot accept new uploads"
                )
            upload = await self.content.begin(user_id, key, request.manifest, "account_directory")
            await self.repository.bind_upload(migration, upload)
            return upload

    async def _require_upload(
        self, user_id: UUID, migration_id: UUID, upload_id: UUID, *, content_required: bool = False
    ) -> tuple[SkillBranchPreparation, SkillContentUpload]:
        """
        先校验真实归属，再允许内容服务处理过期或文件状态。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :param upload_id (UUID): 原租约身份
        :param content_required (bool): 是否请求传输或重新完成内容而非原状态元数据
        :return tuple[SkillBranchPreparation, SkillContentUpload]: 精确授权迁移和上传
        """
        migration = await self.conflicts.require(user_id, migration_id)
        upload = await self.repository.upload(migration, upload_id)
        if upload is None:
            raise SkillContentError("UPLOAD_NOT_FOUND", "migration upload not found")
        if content_required and migration.content_retired_at is not None:
            raise SkillContentError("STATE_EXPIRED", "migration input content has expired")
        return migration, upload

    async def get(self, user_id: UUID, migration_id: UUID, upload_id: UUID) -> SkillContentUpload:
        """
        允许失效冲突恢复原上传状态，不因此授予新的发布权限。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :param upload_id (UUID): 原租约身份
        :return SkillContentUpload: 当前租约状态
        """
        await self._require_upload(user_id, migration_id, upload_id)
        return await self.content.get(user_id, upload_id)

    async def prepare_file(
        self, user_id: UUID, migration_id: UUID, upload_id: UUID, digest: str
    ) -> SkillTreeEntry:
        """
        接收网络正文前检查原上传归属和声明文件。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :param upload_id (UUID): 原租约身份
        :param digest (str): 声明文件摘要
        :return SkillTreeEntry: 有界接收所需文件元数据
        """
        await self._require_upload(user_id, migration_id, upload_id, content_required=True)
        return await self.content.prepare_file(user_id, upload_id, digest)

    async def put_file(
        self, user_id: UUID, migration_id: UUID, upload_id: UUID, digest: str, source: BinaryIO
    ) -> bool:
        """
        只有本迁移绑定租约才能保存声明的完整已验证字节。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :param upload_id (UUID): 原租约身份
        :param digest (str): 声明文件摘要
        :param source (BinaryIO): 私有有界暂存流
        :return bool: 是否新增实际文件对象
        """
        await self._require_upload(user_id, migration_id, upload_id, content_required=True)
        return await self.content.put_file(user_id, upload_id, digest, source)

    async def complete(self, user_id: UUID, migration_id: UUID, upload_id: UUID) -> SkillStoredTree:
        """
        实际字节、租约提交和迁移授权一起完成，失败回滚所有数据库变更。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :param upload_id (UUID): 原租约身份
        :return SkillStoredTree: 完整人工状态树，不表示迁移完成
        """
        async with self._session.begin_nested():
            await self.conflicts.queries.library.lock_library(user_id)
            migration, upload = await self._require_upload(
                user_id, migration_id, upload_id, content_required=True
            )
            await self._verification.verify(
                user_id, SkillTreeManifest.model_validate(upload.manifest_json)
            )
            tree = await self.content.complete(user_id, upload_id)
            await self.repository.retain_content(migration, tree.digest)
            return tree

    async def plan(self, user_id: UUID, migration_id: UUID) -> SkillMigrationResolutionPlanView:
        """
        没有计划时只返回零版本，不初始化计划、内容或分支。

        :param user_id (UUID): 当前用户
        :param migration_id (UUID): 原始迁移身份
        :return SkillMigrationResolutionPlanView: 已保存选择及当前失效状态
        """
        migration = await self.conflicts.require(user_id, migration_id)
        plan = await self.repository.plan(migration)
        return SkillMigrationResolutionPlanView.model_validate(
            {
                "migration_id": migration.id,
                "current_status": migration.status,
                "revision": plan.revision if plan else 0,
                "choices": [choice_spec(row) for row in await self.repository.choices(migration)],
            }
        )
