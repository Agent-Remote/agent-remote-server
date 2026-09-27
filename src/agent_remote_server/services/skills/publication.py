"""
发布已持久化的完整会话输入，原子保存冲突或归档失效写入。
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_publications import SkillPublication, SkillPublicationBranch
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.finalization_checkpoints import (
    candidate_skill_names,
    valid_candidate_names,
    validate_finalization_limits,
)
from agent_remote_server.services.skills.local_candidates import LocalSkillCandidateService
from agent_remote_server.services.skills.publication_apply import PublicationApply
from agent_remote_server.services.skills.publication_context import (
    PublicationContext,
    directory_members,
)
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.directory_merge import merge_directory_manifests
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillPublicationService:
    """
    内部发布入口，调用方提交事务后才能向 Node 确认完整保存结果。
    """

    def __init__(
        self,
        session: AsyncSession,
        store: PrivateObjectStore,
        policy: SkillStoragePolicy,
    ) -> None:
        """
        所有引用、候选和 head 共用一个事务与用户存储锁。

        :param session (AsyncSession): 外层请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 部署额度
        """
        self._session = session
        self._context = PublicationContext(
            SkillLibraryRepository(session),
            SkillPublicationRepository(session),
            SkillRuntimeRepository(session),
            SkillContentService(session, store, policy),
        )
        self._candidates = LocalSkillCandidateService(session, store, policy)
        self._apply = PublicationApply(self._context, store)
        self._policy = policy
        self._store = store

    async def publish(self, user_id: UUID, finalization_id: UUID) -> SkillPublication:
        """
        同一收尾重试返回已有结果，不把尚未解决的冲突当成可重试覆盖。

        :param user_id (UUID): 已认证的原始内容所有者
        :param finalization_id (UUID): 已持久化收尾身份
        :return SkillPublication: 已发布、完整冲突或整体归档结果
        """
        async with retention_mutation(self._session, user_id):
            context = self._context
            await context.library.lock_library(user_id)
            receipt = await context.repository.receipt(user_id, finalization_id)
            if receipt is None:
                raise SkillContentError("FINALIZATION_NOT_FOUND", "finalization not found")
            previous = await context.repository.latest(receipt)
            if previous is not None:
                return previous
            if receipt.status not in {"persisted", "persisted_unclean"}:
                raise SkillContentError("STATE_PENDING", "input is not ready for first publication")
            return await self._publish(receipt, 1)

    async def recompute(self, user_id: UUID, publication_id: UUID) -> SkillPublication:
        """
        显式取代陈旧目标并从原始完整输入重新比较，不复用任何旧解决选择。

        :param user_id (UUID): 当前用户
        :param publication_id (UUID): 已授权旧冲突尝试
        :return SkillPublication: 新目标上的发布、冲突或整体归档结果
        """
        async with retention_mutation(self._session, user_id):
            context = self._context
            await context.library.lock_library(user_id)
            old = await context.repository.attempt(user_id, publication_id)
            if old is None:
                raise SkillContentError("CONFLICT_NOT_FOUND", "conflict not found")
            receipt = await context.repository.receipt(user_id, old.finalization_id)
            assert receipt is not None
            latest = await context.repository.latest(receipt)
            assert latest is not None
            if latest.id != old.id:
                return latest
            if old.content_retired_at is not None or receipt.content_retired_at is not None:
                raise SkillContentError("STATE_EXPIRED", "publication input has expired")
            invalidated = old.status == "superseded" and old.reason in {
                "state_reset",
                "state_restore",
            }
            if old.status != "conflicted" and not invalidated:
                raise SkillContentError(
                    "CONFLICT_NOT_ACTIVE", "attempt is not an unresolved conflict"
                )
            if old.attempt >= 2**63 - 1:
                raise SkillContentError("LIMIT_EXCEEDED", "publication attempts exhausted")
            old.status = "superseded"
            if not invalidated:
                old.reason = "target_changed"
            receipt.status = "persisted"
            await context.runtime.flush()
            return await self._publish(receipt, old.attempt + 1)

    async def _publish(self, receipt: SkillFinalization, attempt: int) -> SkillPublication:
        """
        对固定原始输入计算一份新的完整尝试。

        :param receipt (SkillFinalization): 已授权完整输入
        :param attempt (int): 当前尝试序号
        :return SkillPublication: 原子发布或保留的失败结果
        """
        context = self._context
        user_id = receipt.user_id
        snapshot = await context.repository.snapshot(receipt)
        directory = await context.runtime.directory(user_id, receipt.account_id)
        if receipt.unclean:
            return await self._detached(receipt, snapshot, "unclean", attempt)
        if directory is None or directory.mode != "managed_v1":
            return await self._detached(receipt, snapshot, "directory_not_managed", attempt)
        if directory.epoch != snapshot.directory_epoch:
            return await self._detached(receipt, snapshot, "directory_epoch_changed", attempt)
        if directory.head_checkpoint_id is None:
            return await self._detached(receipt, snapshot, "directory_expired", attempt)
        head = await context.runtime.checkpoint(
            user_id, receipt.account_id, directory.head_checkpoint_id
        )
        if head is None or not head.retained or head.tree_digest is None:
            return await self._detached(receipt, snapshot, "directory_expired", attempt)
        assert receipt.tree_digest is not None
        base = await context.content.read_tree(user_id, "state", snapshot.tree_digest)
        incoming = await context.content.read_tree(user_id, "state", receipt.tree_digest)
        if incoming == base:
            publication = self._attempt(receipt, directory, head.tree_digest, attempt)
            publication.status = "published"
            publication.result_checkpoint_id = head.id
            receipt.status = "published"
            context.runtime.add(publication)
            await context.runtime.flush()
            return publication
        branches, reason = await context.branches(snapshot, base, incoming)
        if reason is not None:
            return await self._detached(receipt, snapshot, reason, attempt)
        directory_tree = await context.content.read_tree(user_id, "state", head.tree_digest)
        current, invalid_projection = await context.current_tree(user_id, directory_tree, branches)
        members = directory_members(list(await context.runtime.members(head)), branches)
        known = {branch.item.entry_name for branch in branches}
        candidates, source_conflicts = await self._new_candidates(receipt, incoming, known)
        names = set(members) | known | {item.name for item in candidates}
        validate_finalization_limits(current, names, self._policy)
        current_digest = await self._store_tree(receipt, "current", current, attempt)
        publication = self._attempt(receipt, directory, current_digest, attempt)
        result = merge_directory_manifests(base, current, incoming, names)
        conflicts = list(result.conflicts) + source_conflicts
        if invalid_projection:
            conflicts.append(
                SkillMergeConflict(
                    path=".", reason="invalid_tree", unit=tuple(sorted(names | {"."}))
                )
            )
        if conflicts:
            publication.status = "conflicted"
            publication.conflicts_json = [item.model_dump(mode="json") for item in conflicts]
            receipt.status = "conflicted"
        else:
            assert result.merged is not None
            valid_names = await valid_candidate_names(self._store, user_id, result.merged)
            candidates = [item for item in candidates if item.name in valid_names]
            validate_finalization_limits(
                result.merged,
                set(members) | known | {item.name for item in candidates},
                self._policy,
            )
            digest = await self._store_tree(receipt, "result", result.merged, attempt)
            checkpoint = await self._apply.publish(
                snapshot,
                directory,
                result.merged,
                digest,
                branches,
                members,
                candidates,
            )
            publication.status = "published"
            publication.result_checkpoint_id = checkpoint.id
            receipt.status = "published"
        context.runtime.add(publication)
        await context.runtime.flush()
        for branch in branches:
            context.runtime.add(
                SkillPublicationBranch(
                    publication_id=publication.id,
                    state_id=branch.state.id,
                    user_id=user_id,
                    account_id=receipt.account_id,
                    entry_name=branch.item.entry_name,
                    state_epoch=branch.state.epoch,
                    expected_checkpoint_id=branch.state.head_checkpoint_id,
                    changed=branch.changed,
                )
            )
        await context.runtime.flush()
        return publication

    async def _new_candidates(
        self,
        receipt: SkillFinalization,
        incoming: SkillTreeManifest,
        known: set[str],
    ) -> tuple[list[AccountLocalSkill], list[SkillMergeConflict]]:
        """
        新生成有效技能保留独立候选，同名现有来源只能显式解决。

        :param receipt (SkillFinalization): 已保存输入
        :param incoming (SkillTreeManifest): 完整会话树
        :param known (set[str]): 原快照实际暴露的身份名称
        :return tuple[list[AccountLocalSkill], list[SkillMergeConflict]]: 待激活来源及身份冲突
        """
        assert receipt.checkpoint_id is not None
        occupied = {
            item.name for item in await self._context.library.list_installations(receipt.user_id)
        }
        occupied |= await self._context.repository.active_local_names(
            receipt.user_id, receipt.account_id
        )
        candidates = []
        conflicts = []
        for name in sorted(candidate_skill_names(incoming) - known):
            try:
                candidate = await self._candidates.register(
                    receipt.user_id, receipt.account_id, receipt.checkpoint_id, name
                )
            except SkillContentError as error:
                if error.code in {
                    "INVALID_SKILL_FORMAT",
                    "SOURCE_LAYOUT_CHANGED",
                    "INVALID_SKILL_NAME",
                }:
                    continue
                raise
            candidates.append(candidate)
            if name in occupied:
                conflicts.append(
                    SkillMergeConflict(path=name, reason="source_conflict", unit=(name,))
                )
        return candidates, conflicts

    async def _store_tree(
        self, receipt: SkillFinalization, label: str, tree: SkillTreeManifest, attempt: int
    ) -> str:
        """
        保存比较或结果完整树，不使用另一个用户的同摘要内容。

        :param receipt (SkillFinalization): 发布归属
        :param label (str): 此次尝试的固定树用途
        :param tree (SkillTreeManifest): 已验证完整树
        :param attempt (int): 当前发布尝试序号
        :return str: 已持久化状态树摘要
        """
        upload = await self._context.content.begin(
            receipt.user_id,
            f"publication:{receipt.id}:{attempt}:{label}",
            tree,
            "account_directory",
        )
        return (await self._context.content.complete(receipt.user_id, upload.id)).digest

    def _attempt(
        self,
        receipt: SkillFinalization,
        directory: AccountSkillDirectoryState,
        current_digest: str,
        attempt: int,
    ) -> SkillPublication:
        """
        建立尚未 flush 的尝试，完整结果确定后才写入约束表。

        :param receipt (SkillFinalization): 原始收尾
        :param directory (AccountSkillDirectoryState): 当前目录前置条件
        :param current_digest (str): 已保留比较树
        :param attempt (int): 当前发布尝试序号
        :return SkillPublication: 待填写终态的尝试
        """
        return SkillPublication(
            id=uuid4(),
            user_id=receipt.user_id,
            account_id=receipt.account_id,
            finalization_id=receipt.id,
            attempt=attempt,
            status="conflicted",
            directory_epoch=directory.epoch,
            expected_directory_id=directory.head_checkpoint_id,
            current_tree_digest=current_digest,
            conflicts_json=[],
        )

    async def _detached(
        self,
        receipt: SkillFinalization,
        snapshot: SessionSkillSnapshot,
        reason: str,
        attempt: int,
    ) -> SkillPublication:
        """
        归档整个输入，不更新目录、分支或任何本地来源。

        :param receipt (SkillFinalization): 完整输入
        :param snapshot (SessionSkillSnapshot): 原始快照
        :param reason (str): 稳定归档原因
        :param attempt (int): 当前发布尝试序号
        :return SkillPublication: 保存恢复引用的归档尝试
        """
        publication = SkillPublication(
            id=uuid4(),
            user_id=receipt.user_id,
            account_id=receipt.account_id,
            finalization_id=receipt.id,
            attempt=attempt,
            status="detached",
            reason=reason,
            directory_epoch=snapshot.directory_epoch,
            conflicts_json=[],
        )
        receipt.status = "detached"
        self._context.runtime.add(publication)
        await self._context.runtime.flush()
        return publication
