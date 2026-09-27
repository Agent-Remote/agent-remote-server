"""
读取当前用户的完整冲突来源、计划和有界三侧元数据差异。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_resolution import SkillResolutionRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_conflicts import (
    SkillConflictBranch,
    SkillConflictDiff,
    SkillConflictInput,
    SkillConflictPage,
    SkillConflictPathDiff,
    SkillConflictSummary,
    SkillConflictView,
    SkillResolutionView,
)
from agent_remote_server.schemas.skill_manifest import validate_relative_path
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.resolution_choices import choice_spec
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillConflictService:
    """
    查询不自动创建解决计划，所有关联数据保持同用户账户范围。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        在请求事务内共享授权与存储锁。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有对象卷
        :param policy (SkillStoragePolicy): 存储额度
        """
        self._library = SkillLibraryRepository(session)
        self._publication = SkillPublicationRepository(session)
        self._runtime = SkillRuntimeRepository(session)
        self._repository = SkillResolutionRepository(session)
        self._content = SkillContentService(session, store, policy)
        self._queries = SkillStateQueryService(session, store, policy)

    async def require(self, user_id: UUID, publication_id: UUID) -> SkillPublication:
        """
        每次请求独立授权已保存尝试。

        :param user_id (UUID): 认证所有者
        :param publication_id (UUID): 待读取尝试
        :return SkillPublication: 同用户尝试
        """
        await self._library.read_library(user_id)
        result = await self._repository.publication(user_id, publication_id)
        if result is None:
            raise SkillContentError("CONFLICT_NOT_FOUND", "conflict not found")
        return result

    async def list(
        self,
        user_id: UUID,
        account_id: UUID,
        limit: int = 100,
        cursor: UUID | None = None,
        skill: str | None = None,
    ) -> SkillConflictPage:
        """
        账户和游标均需属于当前用户，防止用翻页推测其他账户记录。

        :param user_id (UUID): 认证所有者
        :param account_id (UUID): 明确账户范围
        :param limit (int): 有界页大小
        :param cursor (UUID | None): 同账户上一页末尾身份
        :param skill (str | None): 可选名称或稳定来源身份
        :return SkillConflictPage: 冲突历史页
        """
        if not 1 <= limit <= 200:
            raise SkillContentError("INVALID_REQUEST", "page size is out of range")
        skill_id = await self._queries.scope(
            user_id, account_id, "item" if skill is not None else "account-directory", skill
        )
        before = await self.require(user_id, cursor) if cursor is not None else None
        if before is not None and (
            before.account_id != account_id
            or (skill_id is not None and not await self._repository.has_source(before, skill_id))
        ):
            raise SkillContentError("CONFLICT_NOT_FOUND", "cursor not found in this scope")
        rows = list(
            await self._repository.publications(user_id, account_id, limit + 1, before, skill_id)
        )
        return SkillConflictPage(
            items=[summary(row) for row in rows[:limit]],
            next_cursor=rows[limit - 1].id if len(rows) > limit else None,
        )

    async def info(self, user_id: UUID, publication_id: UUID) -> SkillConflictView:
        """
        原始物化树与初始目录 head 分开说明，所有来源和选择均可解释。

        :param user_id (UUID): 认证所有者
        :param publication_id (UUID): 明确尝试身份
        :return SkillConflictView: 完整元数据及计划状态
        """
        publication = await self.require(user_id, publication_id)
        receipt = await self._publication.receipt(user_id, publication.finalization_id)
        assert receipt is not None
        snapshot = await self._publication.snapshot(receipt)
        latest = await self._publication.latest(receipt)
        items = {item.state_id: item for item in await self._runtime.snapshot_items(snapshot)}
        branches = [
            SkillConflictBranch(
                state_id=row.state_id,
                entry_name=row.entry_name,
                state_epoch=row.state_epoch,
                checkpoint_id=row.expected_checkpoint_id,
                changed=row.changed,
                revision_id=UUID(str(items[row.state_id].resolution_json["revision_id"])),
            )
            for row in await self._repository.branches(publication)
        ]
        plan = await self._repository.plan(publication)
        return SkillConflictView(
            **summary(publication).model_dump(),
            session_reference_id=snapshot.session_reference_id,
            replacement_id=latest.id
            if latest is not None and latest.id != publication_id
            else None,
            base=SkillConflictInput(
                source="session_snapshot",
                reference_id=snapshot.id,
                tree_digest=snapshot.tree_digest
                if publication.content_retired_at is None and snapshot.content_retired_at is None
                else None,
            ),
            current=SkillConflictInput(
                source="publication_comparison",
                reference_id=publication.id,
                tree_digest=publication.current_tree_digest
                if publication.content_retired_at is None
                else None,
            ),
            incoming=SkillConflictInput(
                source="finalization",
                reference_id=receipt.id,
                tree_digest=receipt.tree_digest
                if publication.content_retired_at is None and receipt.content_retired_at is None
                else None,
            ),
            branches=branches,
            conflicts=tuple(
                SkillMergeConflict.model_validate(item) for item in publication.conflicts_json
            ),
            plan_revision=plan.revision if plan is not None else 0,
            choices=[choice_spec(row) for row in await self._repository.choices(publication)],
        )

    async def diff(
        self,
        user_id: UUID,
        publication_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SkillConflictDiff:
        """
        只返回有差异路径的三侧摘要和权限元数据，不读取或猜测合并正文。

        :param user_id (UUID): 认证所有者
        :param publication_id (UUID): 原始尝试
        :param limit (int): 有界路径页大小
        :param cursor (str | None): 上一页末尾相对路径
        :return SkillConflictDiff: 完整明确三侧的元数据差异页
        """
        if not 1 <= limit <= 500:
            raise SkillContentError("INVALID_REQUEST", "diff page size is out of range")
        if cursor is not None:
            try:
                validate_relative_path(cursor)
            except ValueError as error:
                raise SkillContentError("INVALID_REQUEST", "invalid diff cursor") from error
        info = await self.info(user_id, publication_id)
        sides = []
        for side in (info.base, info.current, info.incoming):
            if side.tree_digest is None:
                raise SkillContentError("STATE_EXPIRED", "comparison tree is not retained")
            tree = await self._content.read_tree(user_id, "state", side.tree_digest)
            sides.append({entry.path: entry for entry in tree.entries})
        paths = sorted(set().union(*(side.keys() for side in sides)), key=str.encode)
        items = []
        for path in paths:
            if cursor is not None and path.encode() <= cursor.encode():
                continue
            base, current, incoming = (side.get(path) for side in sides)
            if base == current == incoming:
                continue
            items.append(
                SkillConflictPathDiff(path=path, base=base, current=current, incoming=incoming)
            )
            if len(items) > limit:
                break
        return SkillConflictDiff(
            publication_id=publication_id,
            items=items[:limit],
            next_cursor=items[limit - 1].path if len(items) > limit else None,
        )

    async def operation_by_id(self, user_id: UUID, operation_id: UUID) -> SkillResolutionView:
        """
        从认证所有者的不可变记录读取原受理结果。

        :param user_id (UUID): 当前用户
        :param operation_id (UUID): 原始解决受理身份
        :return SkillResolutionView: 原响应或明确不存在错误
        """
        row = await self._repository.operation_by_id(user_id, operation_id)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "resolution operation not found")
        return SkillResolutionView.model_validate(row.response_json)

    async def operation(self, user_id: UUID, key: str) -> SkillResolutionView:
        """
        断线后只凭持久化键查询原结果，不重放旧计划选择。

        :param user_id (UUID): 当前用户
        :param key (str): 原始命令键
        :return SkillResolutionView: 不可变原接受结果
        """
        row = await self._repository.operation(user_id, key)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "resolution operation not found")
        return SkillResolutionView.model_validate(row.response_json)


def summary(publication: SkillPublication) -> SkillConflictSummary:
    """
    提取不包含任何正文的明确目录尝试摘要。

    :param publication (SkillPublication): 已授权尝试
    :return SkillConflictSummary: 安全摘要
    """
    return SkillConflictSummary(
        id=publication.id,
        account_id=publication.account_id,
        finalization_id=publication.finalization_id,
        attempt=publication.attempt,
        status=publication.status,
        reason=publication.reason,
    )
