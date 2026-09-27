"""
在一致用户读锁下解释状态历史并导出完整依赖单元。
"""

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_state_queries import SkillStateQueryRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_state_queries import (
    SkillCheckpointMember,
    SkillCheckpointMemberPage,
    SkillCheckpointPage,
    SkillCheckpointTree,
    SkillCheckpointView,
    SkillStatePendingPage,
    SkillStatePendingView,
)
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.skill_manager.directory_merge import directory_merge_units
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillStateQueryService:
    """
    读取不初始化分支、不推进库代数，也不把待上传内容表示为完整快照。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        在外层事务共享内容授权与历史仓储。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 存储策略
        """
        self.repository = SkillStateQueryRepository(session)
        self.library = SkillLibraryRepository(session)
        self.runtime = SkillRuntimeRepository(session)
        self.content = SkillContentService(session, store, policy)

    async def require(self, user_id: UUID, checkpoint_id: UUID) -> SkillCheckpoint:
        """
        只读锁不为没有技能内容的新用户建立任何状态。

        :param user_id (UUID): 当前认证用户
        :param checkpoint_id (UUID): 检查点身份
        :return SkillCheckpoint: 同用户的检查点或内容墓碑
        """
        await self.library.read_library(user_id)
        checkpoint = await self.repository.checkpoint(user_id, checkpoint_id)
        if checkpoint is None:
            raise SkillContentError("CHECKPOINT_NOT_FOUND", "checkpoint not found")
        return checkpoint

    async def scope(
        self,
        user_id: UUID,
        account_id: UUID,
        scope: Literal["item", "account-directory"],
        skill: str | None,
    ) -> UUID | None:
        """
        库与本地来源同名要求稳定身份，完整目录范围不能同时提供技能。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 指定账户
        :param scope (Literal["item", "account-directory"]): 明确对象范围
        :param skill (str | None): 单项名称或稳定身份
        :return UUID | None: 已授权稳定来源身份或完整目录范围
        """
        await self.library.read_library(user_id)
        if await self.library.account(user_id, account_id) is None:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "account not found")
        if (scope == "item") != (skill is not None):
            raise SkillContentError(
                "INVALID_REQUEST", "select one skill or the account-directory scope"
            )
        if skill is None:
            return None
        library = await self.library.installation(user_id, skill)
        local = await self.repository.local_source(user_id, account_id, skill)
        if library is not None and local is not None:
            raise SkillContentError(
                "SKILL_SOURCE_CONFLICT", "ambiguous name; use the stable skill ID"
            )
        source = library or local
        if source is None:
            raise SkillContentError("SKILL_NOT_FOUND", "skill not found in this account scope")
        return source.id

    async def list(
        self,
        user_id: UUID,
        account_id: UUID,
        scope: Literal["item", "account-directory"],
        skill: str | None = None,
        limit: int = 100,
        cursor: UUID | None = None,
    ) -> SkillCheckpointPage:
        """
        列出所有版本与纪元的有界历史，跨账户或来源的游标不可替用。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 指定账户
        :param scope (Literal["item", "account-directory"]): 明确对象范围
        :param skill (str | None): 单项来源选择
        :param limit (int): 页大小
        :param cursor (UUID | None): 上页末尾检查点
        :return SkillCheckpointPage: 稳定历史页
        """
        _page_size(limit)
        skill_id = await self.scope(user_id, account_id, scope, skill)
        before = await self.require(user_id, cursor) if cursor is not None else None
        if before is not None:
            branch = await self.repository.branch(before)
            actual_skill = (branch.installation_id or branch.local_skill_id) if branch else None
            if before.account_id != account_id or actual_skill != skill_id:
                raise SkillContentError("CHECKPOINT_NOT_FOUND", "cursor not found in this scope")
        rows = list(
            await self.repository.checkpoints(user_id, account_id, skill_id, limit + 1, before)
        )
        return SkillCheckpointPage(
            items=[await self.view(row) for row in rows[:limit]],
            next_cursor=rows[limit - 1].id if len(rows) > limit else None,
        )

    async def view(self, checkpoint: SkillCheckpoint) -> SkillCheckpointView:
        """
        显式标注原始分支身份、当前 head 与来源会话的独立结果。

        :param checkpoint (SkillCheckpoint): 已授权历史对象
        :return SkillCheckpointView: 不含内容正文或宿主路径的元数据
        """
        branch = await self.repository.branch(checkpoint)
        directory = await self.runtime.directory(checkpoint.user_id, checkpoint.account_id)
        receipt = await self.repository.source_finalization(checkpoint)
        head = (
            branch.head_checkpoint_id
            if branch
            else (directory.head_checkpoint_id if directory else None)
        )
        return SkillCheckpointView(
            id=checkpoint.id,
            account_id=checkpoint.account_id,
            scope="item" if branch else "account-directory",
            state_id=checkpoint.state_id,
            skill_id=(branch.installation_id or branch.local_skill_id) if branch else None,
            origin=("user_library" if branch.installation_id else "account_local")
            if branch
            else None,
            revision_id=(branch.base_revision_id or branch.local_revision_id) if branch else None,
            installation_epoch=branch.installation_epoch if branch else None,
            state_epoch=checkpoint.state_epoch,
            directory_epoch=checkpoint.directory_epoch,
            backing_directory_id=checkpoint.backing_directory_id,
            current_state_epoch=branch.epoch if branch else None,
            current_directory_epoch=directory.epoch if directory else None,
            subtree_prefix=checkpoint.subtree_prefix,
            parent_id=checkpoint.parent_id,
            content_digest=checkpoint.content_digest,
            retained=checkpoint.retained,
            is_head=checkpoint.id == head,
            invalid_skill_format=checkpoint.invalid_skill_format,
            source_session_reference_id=checkpoint.source_session_reference_id,
            finalization_id=receipt.id if receipt else None,
            finalization_status=receipt.status if receipt else None,
            storage_location="server" if checkpoint.retained else "expired",
            created_at=checkpoint.created_at,
        )

    async def members(
        self, user_id: UUID, checkpoint_id: UUID, limit: int = 100, cursor: str | None = None
    ) -> SkillCheckpointMemberPage:
        """
        目录检查点引用按原成员返回，不随当前版本选择改变。

        :param user_id (UUID): 当前用户
        :param checkpoint_id (UUID): 完整目录检查点
        :param limit (int): 页大小
        :param cursor (str | None): 上页末尾成员名称
        :return SkillCheckpointMemberPage: 有界不可变成员引用
        """
        _page_size(limit)
        checkpoint = await self.require(user_id, checkpoint_id)
        if checkpoint.scope != "directory":
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "members require an account-directory checkpoint"
            )
        rows = await self.repository.members(checkpoint, limit + 1, cursor)
        items = []
        for member, branch, view in rows[:limit]:
            skill_id = branch.installation_id or branch.local_skill_id
            revision_id = branch.base_revision_id or branch.local_revision_id
            assert skill_id is not None and revision_id is not None
            items.append(
                SkillCheckpointMember(
                    entry_name=member.entry_name,
                    state_id=branch.id,
                    checkpoint_id=member.checkpoint_id,
                    skill_id=skill_id,
                    origin="user_library" if branch.installation_id else "account_local",
                    revision_id=revision_id,
                    installation_epoch=branch.installation_epoch,
                    state_epoch=view.state_epoch,
                )
            )
        return SkillCheckpointMemberPage(
            checkpoint_id=checkpoint.id,
            items=items,
            next_cursor=rows[limit - 1][0].entry_name if len(rows) > limit else None,
        )

    async def tree(self, user_id: UUID, checkpoint_id: UUID) -> SkillCheckpointTree:
        """
        导出原路径和必要依赖，不为过期或不完整内容构造空树。

        :param user_id (UUID): 当前用户
        :param checkpoint_id (UUID): 明确检查点
        :return SkillCheckpointTree: 可独立验证的完整导出单元
        """
        checkpoint = await self.require(user_id, checkpoint_id)
        if not checkpoint.retained or checkpoint.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "checkpoint content is no longer retained")
        original = await self.content.read_tree(user_id, "state", checkpoint.tree_digest)
        roots = {entry.path.split("/", 1)[0] for entry in original.entries}
        prefix = checkpoint.subtree_prefix
        dependencies: tuple[str, ...] = ()
        tree = original
        if prefix:
            unit = next(
                (unit for unit in directory_merge_units((original,), roots) if prefix in unit), ()
            )
            dependencies = tuple(name for name in unit if name != prefix)
            tree = SkillTreeManifest(
                entries=tuple(
                    entry for entry in original.entries if entry.path.split("/", 1)[0] in unit
                )
            )
        return SkillCheckpointTree(
            checkpoint_id=checkpoint.id,
            source_tree_digest=checkpoint.tree_digest,
            tree_digest=manifest_digest(tree),
            subtree_prefix=prefix,
            dependency_roots=dependencies,
            locally_removed=bool(prefix and prefix not in roots),
            manifest=tree,
        )

    async def pending(
        self,
        user_id: UUID,
        account_id: UUID,
        scope: Literal["item", "account-directory"],
        skill: str | None = None,
        limit: int = 100,
        cursor: UUID | None = None,
    ) -> SkillStatePendingPage:
        """
        返回尚无完整内容的收尾，离线节点不能因此伪装为可导出。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 指定账户
        :param scope (Literal["item", "account-directory"]): 明确范围
        :param skill (str | None): 单项来源选择
        :param limit (int): 页大小
        :param cursor (UUID | None): 上页末尾收尾身份
        :return SkillStatePendingPage: 独立待上传记录页
        """
        _page_size(limit)
        skill_id = await self.scope(user_id, account_id, scope, skill)
        if cursor is not None and not await self.repository.pending_cursor_valid(
            user_id, account_id, skill_id, cursor
        ):
            raise SkillContentError("FINALIZATION_NOT_FOUND", "cursor not found in this scope")
        rows = await self.repository.pending(user_id, account_id, skill_id, limit + 1, cursor)
        return SkillStatePendingPage(
            items=[
                SkillStatePendingView(
                    id=row.id,
                    snapshot_id=snapshot.id,
                    session_reference_id=snapshot.session_reference_id,
                    node_id=snapshot.node_id,
                    incoming_digest=row.incoming_digest,
                )
                for row, snapshot in rows[:limit]
            ],
            next_cursor=rows[limit - 1][0].id if len(rows) > limit else None,
        )


def _page_size(limit: int) -> None:
    """
    服务调用也必须服从有界页大小，不只依赖 HTTP 校验。

    :param limit (int): 页大小
    """
    if not 1 <= limit <= 200:
        raise SkillContentError("INVALID_REQUEST", "page size is out of range")
