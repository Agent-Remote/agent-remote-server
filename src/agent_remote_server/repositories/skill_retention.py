"""
读取单用户有界引用索引，保活语义由服务与纯算法决定。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only, undefer
from sqlalchemy.orm.interfaces import ORMOption

from agent_remote_server.db import Base
from agent_remote_server.models import Session, ToolAccount
from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_discovery import (
    SkillDeploymentDiscoveredSource,
    SkillDeploymentDiscovery,
)
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import (
    SkillAccountOverride,
    SkillInstallation,
    SkillOperation,
    SkillRevision,
    SkillToolOverride,
)
from agent_remote_server.models.skill_local import AccountLocalSkill, AccountLocalSkillRevision
from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionContent,
    SkillMigrationResolutionUpload,
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_prune_claims import SkillPruneContentClaim
from agent_remote_server.models.skill_publications import SkillPublication, SkillPublicationBranch
from agent_remote_server.models.skill_resolution import SkillResolutionChoice
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
    SkillFinalization,
)
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.models.skill_storage import (
    SkillContentUpload,
    SkillStoredTree,
    SkillTreeObjectReference,
)
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.skill_deployment_tasks import (
    DeploymentTaskState,
    SkillDeploymentTaskRepository,
)
from agent_remote_server.repositories.skill_retention_schema import (
    require_classified_schema,
    require_classified_tree_references,
)
from agent_remote_server.repositories.skill_upload_retention import (
    UploadObjectReference,
    upload_references,
)


@dataclass(frozen=True)
class RetentionIndex:
    """
    所有集合共享认证用户和外层事务，不借助名称推断来源身份。
    """

    user_id: UUID
    accounts: tuple[ToolAccount, ...]
    sessions: tuple[Session, ...]
    installations: tuple[SkillInstallation, ...]
    library_operations: tuple[SkillOperation, ...]
    deployment_targets: tuple[SkillDeploymentTarget, ...]
    deployment_entries: tuple[SkillDeploymentEntry, ...]
    deployment_attempts: tuple[SkillDeploymentAttempt, ...]
    deployment_tasks: tuple[SkillDeploymentTask, ...]
    deployment_discoveries: tuple[SkillDeploymentDiscovery, ...]
    deployment_discovered_sources: tuple[SkillDeploymentDiscoveredSource, ...]
    deployment_node_tasks: tuple[DeploymentTaskState, ...]
    revisions: tuple[SkillRevision, ...]
    tools: tuple[SkillToolOverride, ...]
    overrides: tuple[SkillAccountOverride, ...]
    locals: tuple[AccountLocalSkill, ...]
    local_revisions: tuple[AccountLocalSkillRevision, ...]
    directories: tuple[AccountSkillDirectoryState, ...]
    branches: tuple[AccountSkillState, ...]
    checkpoints: tuple[SkillCheckpoint, ...]
    members: tuple[SkillDirectoryMember, ...]
    snapshots: tuple[SessionSkillSnapshot, ...]
    snapshot_items: tuple[SessionSkillSnapshotItem, ...]
    finalizations: tuple[SkillFinalization, ...]
    publications: tuple[SkillPublication, ...]
    publication_branches: tuple[SkillPublicationBranch, ...]
    choices: tuple[SkillResolutionChoice, ...]
    migrations: tuple[SkillBranchPreparation, ...]
    migration_content: tuple[SkillMigrationResolutionContent, ...]
    takeovers: tuple[SkillAccountTakeover, ...]
    uploads: tuple[SkillContentUpload, ...]
    upload_objects: tuple[UploadObjectReference, ...]
    tree_objects: tuple[SkillTreeObjectReference, ...]
    trees: tuple[SkillStoredTree, ...]
    prune_claims: tuple[SkillPruneContentClaim, ...]


class SkillRetentionRepository:
    """
    不修改任何状态；达到资源边界时整体失败，不返回可误用的部分图。
    """

    def __init__(
        self,
        session: AsyncSession,
        max_rows: int = 1_000_000,
        max_json_characters: int = 16 * 1024 * 1024,
    ) -> None:
        """
        保持外层事务及明确的全索引行数预算。

        :param session (AsyncSession): 已取得用户存储锁的事务
        :param max_rows (int): 完整索引最大总行数
        :param max_json_characters (int): 必需 JSON 文本总字符预算，UTF-8 至多四倍字节
        """
        self._session = session
        self._max_rows = max_rows
        self._max_json_characters = max_json_characters

    async def session_owner(self, session_id: UUID) -> UUID | None:
        """
        已授权会话生命周期按数据库归属取得内容锁，不信任任务中的 user_id。

        :param session_id (UUID): 既有业务入口选定的会话身份
        :return UUID | None: 持久化所有者，缺失会话不建立新记录
        """
        return await self._session.scalar(select(Session.user_id).where(Session.id == session_id))

    async def has_active_migration_upload(
        self, user_id: UUID, migration_ids: tuple[UUID, ...], now: datetime
    ) -> bool:
        """
        保留仍有有效上传消费者的历史，不让退役中断已授权输入传输。

        :param user_id (UUID): 已锁定所有者
        :param migration_ids (tuple[UUID, ...]): 已授权精确迁移集合
        :param now (datetime): 本次退役固定时间
        :return bool: 是否仍存在有效未完成上传
        """
        return bool(await self.active_migration_ids(user_id, migration_ids, now))

    async def active_migration_ids(
        self, user_id: UUID, migration_ids: tuple[UUID, ...], now: datetime
    ) -> frozenset[UUID]:
        """
        完整读取选定比较的有效输入上传消费者，分块查询但不截断结果或提前退休历史。

        :param user_id (UUID): 已锁定所有者
        :param migration_ids (tuple[UUID, ...]): 同用户完整精确迁移集合
        :param now (datetime): 单次计划固定真实时间
        :return frozenset[UUID]: 仍有未过期输入上传的精确迁移身份
        """
        if len(migration_ids) > self._max_rows:
            raise ValueError("retention migration identity limit exceeded")
        result: set[UUID] = set()
        for offset in range(0, len(migration_ids), 500):
            result.update(
                await self._session.scalars(
                    select(SkillMigrationResolutionUpload.migration_id)
                    .join(
                        SkillContentUpload,
                        SkillContentUpload.id == SkillMigrationResolutionUpload.upload_id,
                    )
                    .where(
                        SkillMigrationResolutionUpload.user_id == user_id,
                        SkillMigrationResolutionUpload.migration_id.in_(
                            migration_ids[offset : offset + 500]
                        ),
                        SkillContentUpload.user_id == user_id,
                        SkillContentUpload.status == "staged",
                        SkillContentUpload.expires_at > now,
                    )
                    .distinct()
                )
            )
        return frozenset(result)

    async def load(self, user_id: UUID) -> RetentionIndex:
        """
        每个查询均显式过滤所有者并刷新可能陈旧的 ORM 身份映射。

        :param user_id (UUID): 当前认证用户
        :return RetentionIndex: 有界完整用户引用集合
        """
        require_classified_schema(Base.metadata)
        require_classified_tree_references(Base.metadata)
        await self._check_metadata_budget(user_id)
        remaining = self._max_rows

        async def owned[T: Base](model: type[T], *options: ORMOption) -> tuple[T, ...]:
            """
            统一执行指定用户表读取，不将其他用户记录放入内存分析。

            :param model (type[T]): 明确包含 user_id 的引用模型
            :param options (ORMOption): 排除不参与引用判断的大型元数据
            :return tuple[T, ...]: 当前用户完整结果
            """
            nonlocal remaining
            rows = tuple(
                await self._session.scalars(
                    select(model)
                    .options(*options)
                    .where(model.__table__.c.user_id == user_id)
                    .limit(remaining + 1)
                    .execution_options(populate_existing=True)
                )
            )
            remaining -= len(rows)
            if remaining < 0:
                raise ValueError("retention index row limit exceeded")
            return rows

        deployment_tasks = await SkillDeploymentTaskRepository(self._session).retention_tasks(
            user_id, remaining + 1
        )
        remaining -= len(deployment_tasks)
        if remaining < 0:
            raise ValueError("retention index row limit exceeded")
        uploads = await owned(
            SkillContentUpload, defer(SkillContentUpload.manifest_json, raiseload=True)
        )
        # 旧版暂存仍须完整校验；终态和已索引清单不参与本图的 JSON 物化。
        (
            await self._session.scalars(
                select(SkillContentUpload)
                .options(undefer(SkillContentUpload.manifest_json))
                .where(
                    SkillContentUpload.user_id == user_id,
                    SkillContentUpload.status == "staged",
                    SkillContentUpload.object_index_version == 0,
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        references = await upload_references(self._session, user_id, uploads, remaining)
        remaining -= len(references)
        return RetentionIndex(
            user_id=user_id,
            accounts=await owned(
                ToolAccount,
                load_only(
                    ToolAccount.id, ToolAccount.user_id, ToolAccount.tool_type, raiseload=True
                ),
            ),
            sessions=await owned(
                Session, load_only(Session.id, Session.user_id, Session.status, raiseload=True)
            ),
            installations=await owned(
                SkillInstallation,
                defer(SkillInstallation.source_json, raiseload=True),
                defer(SkillInstallation.tracking_json, raiseload=True),
            ),
            library_operations=await owned(
                SkillOperation, defer(SkillOperation.request_json, raiseload=True)
            ),
            deployment_targets=await owned(SkillDeploymentTarget),
            deployment_entries=await owned(SkillDeploymentEntry),
            deployment_attempts=await owned(SkillDeploymentAttempt),
            deployment_tasks=await owned(SkillDeploymentTask),
            deployment_discoveries=await owned(SkillDeploymentDiscovery),
            deployment_discovered_sources=await owned(SkillDeploymentDiscoveredSource),
            deployment_node_tasks=deployment_tasks,
            revisions=await owned(
                SkillRevision,
                defer(SkillRevision.provenance_json, raiseload=True),
                defer(SkillRevision.metadata_json, raiseload=True),
            ),
            tools=await owned(SkillToolOverride),
            overrides=await owned(SkillAccountOverride),
            locals=await owned(AccountLocalSkill),
            local_revisions=await owned(
                AccountLocalSkillRevision,
                defer(AccountLocalSkillRevision.metadata_json, raiseload=True),
            ),
            directories=await owned(AccountSkillDirectoryState),
            branches=await owned(AccountSkillState),
            checkpoints=await owned(SkillCheckpoint),
            members=await owned(SkillDirectoryMember),
            snapshots=await owned(
                SessionSkillSnapshot,
                defer(SessionSkillSnapshot.system_releases_json, raiseload=True),
            ),
            snapshot_items=await owned(
                SessionSkillSnapshotItem,
                defer(SessionSkillSnapshotItem.resolution_json, raiseload=True),
            ),
            finalizations=await owned(SkillFinalization),
            publications=await owned(
                SkillPublication, defer(SkillPublication.conflicts_json, raiseload=True)
            ),
            publication_branches=await owned(SkillPublicationBranch),
            choices=await owned(
                SkillResolutionChoice, defer(SkillResolutionChoice.unit_json, raiseload=True)
            ),
            migrations=await owned(
                SkillBranchPreparation, defer(SkillBranchPreparation.response_json, raiseload=True)
            ),
            migration_content=await owned(SkillMigrationResolutionContent),
            takeovers=await owned(
                SkillAccountTakeover, defer(SkillAccountTakeover.inventory_json, raiseload=True)
            ),
            uploads=uploads,
            upload_objects=references,
            tree_objects=await owned(SkillTreeObjectReference),
            prune_claims=await owned(SkillPruneContentClaim),
            trees=await owned(
                SkillStoredTree, defer(SkillStoredTree.manifest_json, raiseload=True)
            ),
        )

    async def _check_metadata_budget(self, user_id: UUID) -> None:
        """
        在解码必需清单和操作元数据之前，由数据库检查整体文本规模。

        :param user_id (UUID): 唯一允许分析的所有者
        """
        queries = (
            select(func.sum(func.length(cast(SkillContentUpload.manifest_json, Text)))).where(
                SkillContentUpload.user_id == user_id,
                SkillContentUpload.status == "staged",
                SkillContentUpload.object_index_version == 0,
            ),
            select(func.sum(func.length(cast(SkillOperation.result_json, Text)))).where(
                SkillOperation.user_id == user_id
            ),
        )
        characters = 0
        for query in queries:
            characters += int(await self._session.scalar(query) or 0)
            if characters > self._max_json_characters:
                raise ValueError("retention metadata budget exceeded")
