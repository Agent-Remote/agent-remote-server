"""
在同一用户锁下验证精确历史资格，并分析当前目录与受保护分支的完整 backing 树。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.compaction.plan import (
    CompactionDirectory,
    CompactionHead,
    CompactionMember,
    CompactionPlan,
    compact_tree,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    validate_finalization_limits,
)
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.history import history_retention
from agent_remote_server.skill_manager.retention.dependencies import MAX_RETIREMENT_IDENTITIES
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass
class CompactionBuilder:
    """
    完整结果超出分析预算时整体拒绝，不截断成员或猜测缺失来源。
    """

    content: MigrationContent
    policy: SkillStoragePolicy

    async def build(
        self,
        index: RetentionIndex,
        account_id: UUID,
        checkpoint_ids: tuple[UUID, ...],
        generation: int,
        *,
        all_unreferenced: bool,
    ) -> CompactionPlan:
        """
        不写入计量、上传或时钟；实际字节验证与提交使用同一路径。

        :param index (RetentionIndex): 已授权用户的完整引用索引
        :param account_id (UUID): 精确账户身份
        :param checkpoint_ids (tuple[UUID, ...]): 已选历史 item 身份
        :param generation (int): 当前库配置代数
        :param all_unreferenced (bool): 是否明确提前结束等待
        :return CompactionPlan: 完整可比较的只读结果
        """
        if (
            not checkpoint_ids
            or len(checkpoint_ids) > MAX_RETIREMENT_IDENTITIES
            or len(set(checkpoint_ids)) != len(checkpoint_ids)
        ):
            raise SkillContentError(
                "INVALID_REQUEST",
                "select distinct item checkpoints within the retention index limit",
            )
        if not any(account.id == account_id for account in index.accounts):
            raise SkillContentError("ACCOUNT_NOT_FOUND", "account not found")
        directory = next((row for row in index.directories if row.account_id == account_id), None)
        if (
            directory is None
            or directory.mode != "managed_v1"
            or directory.head_checkpoint_id is None
        ):
            raise SkillContentError("STATE_NOT_MANAGED", "account directory must be managed")
        now = datetime.now(UTC)
        protected = protection(index, now)
        histories = {row.key: row for row in history_retention(index, protected, self.policy)}
        checkpoints = {row.id: row for row in index.checkpoints if row.account_id == account_id}
        for identity in checkpoint_ids:
            checkpoint = checkpoints.get(identity)
            if checkpoint is None or checkpoint.scope != "item":
                raise SkillContentError(
                    "CHECKPOINT_NOT_FOUND", "item checkpoint not found in this account"
                )
            if not checkpoint.retained or checkpoint.tree_digest is None:
                raise SkillContentError("STATE_EXPIRED", "selected checkpoint has expired")
            history = histories[RetentionKey("checkpoint", str(identity))]
            if history.reasons:
                raise SkillContentError(
                    "STATE_PROTECTED", "selected checkpoint has active protection"
                )
            if not all_unreferenced and (history.expires_at is None or history.expires_at > now):
                raise SkillContentError("HISTORY_NOT_EXPIRED", "selected history has not expired")
        heads = []
        source_ids = {directory.head_checkpoint_id}
        for branch in index.branches:
            if (
                branch.account_id != account_id
                or branch.expired
                or not protected.reasons("branch", branch.id)
            ):
                continue
            if branch.head_checkpoint_id is None:
                continue
            checkpoint = checkpoints[branch.head_checkpoint_id]
            assert checkpoint.tree_digest is not None
            heads.append(
                CompactionHead(
                    state_id=branch.id,
                    checkpoint_id=checkpoint.id,
                    state_epoch=branch.epoch,
                    subtree_prefix=checkpoint.subtree_prefix,
                    tree_digest=checkpoint.tree_digest,
                    backing_directory_id=checkpoint.backing_directory_id,
                    invalid_skill_format=checkpoint.invalid_skill_format,
                )
            )
            if checkpoint.backing_directory_id is not None:
                source_ids.add(checkpoint.backing_directory_id)
        if len(heads) + len(source_ids) > MAX_RETIREMENT_IDENTITIES:
            raise SkillContentError(
                "COMPACTION_LIMIT_EXCEEDED", "too many protected directory views"
            )
        members: dict[UUID, list[CompactionMember]] = {identity: [] for identity in source_ids}
        for member in index.members:
            if member.directory_checkpoint_id in members:
                members[member.directory_checkpoint_id].append(
                    CompactionMember(
                        member.entry_name,
                        member.state_id,
                        member.checkpoint_id,
                    )
                )
        selected_checkpoints = frozenset(checkpoint_ids)
        preserved_by_directory: dict[UUID, set[str]] = {}
        for head in heads:
            if head.backing_directory_id is not None:
                preserved_by_directory.setdefault(head.backing_directory_id, set()).add(
                    head.subtree_prefix
                )
        entries = 0
        verified: set[str] = set()
        directories = []
        for identity in sorted(source_ids):
            checkpoint = checkpoints[identity]
            original = await self.content.tree(checkpoint)
            entries += len(original.entries)
            if entries > 1_000_000:
                raise SkillContentError(
                    "COMPACTION_LIMIT_EXCEEDED", "too many complete manifest entries"
                )
            if checkpoint.tree_digest not in verified:
                await self.content.verify(index.user_id, original)
                assert checkpoint.tree_digest is not None
                verified.add(checkpoint.tree_digest)
            original_members = tuple(sorted(members[identity]))
            preserved = frozenset(preserved_by_directory.get(identity, ()))
            result, removed, blocked = compact_tree(
                original, original_members, selected_checkpoints, preserved
            )
            removed_names = {member.entry_name for member in removed}
            invalid = await self._invalid(
                index.user_id,
                result,
                tuple(
                    member.entry_name
                    for member in original_members
                    if member.entry_name not in removed_names
                ),
            )
            validate_finalization_limits(
                result, {member.entry_name for member in original_members}, self.policy
            )
            directories.append(
                CompactionDirectory(
                    checkpoint_id=checkpoint.id,
                    directory_epoch=checkpoint.directory_epoch,
                    tree_digest=checkpoint.content_digest,
                    members=original_members,
                    original=original,
                    result=result,
                    removed=removed,
                    blocked=blocked,
                    invalid_skill_format=invalid,
                )
            )
        plan = CompactionPlan(
            user_id=index.user_id,
            account_id=account_id,
            checkpoint_ids=tuple(sorted(checkpoint_ids)),
            all_unreferenced=all_unreferenced,
            library_generation=generation,
            directory_head_id=directory.head_checkpoint_id,
            directory_epoch=directory.epoch,
            heads=tuple(sorted(heads, key=lambda row: row.state_id)),
            directories=tuple(directories),
        )
        changed = plan.changed_directories()
        if changed:
            await self.content.queries.content.validate_state_admissions(
                index.user_id,
                tuple(row.result for row in plan.directories if row.checkpoint_id in changed),
            )
        return plan

    async def _invalid(
        self, user_id: UUID, tree: SkillTreeManifest, names: tuple[str, ...]
    ) -> bool:
        """
        整理保留实际文件，完整目录的格式诊断只依据剩余成员，不继承已移除根的错误。

        :param user_id (UUID): 内容所有者
        :param tree (SkillTreeManifest): 完整整理结果
        :param names (tuple[str, ...]): 剩余成员的真实目录名称
        :return bool: 任一剩余成员是否不满足当前技能格式
        """
        for name in names:
            try:
                entry = _skill_document(tree, name + "/SKILL.md")
                _metadata(await self.content.store.read_prefix(user_id, entry, 65_544), name)
            except SkillContentError:
                return True
        return False
