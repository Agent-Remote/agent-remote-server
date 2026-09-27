"""
把已发布的账户本地来源纳入精确快照，保留独立身份与原始目录视图。
"""

from dataclasses import dataclass
from uuid import uuid4

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.snapshot_branches import PreparedSkill, SnapshotBranches
from agent_remote_server.skill_manager.materialization import SkillSubtree


@dataclass
class SnapshotLocalBranches:
    """
    复用快照事务的内容、检查点与用户锁，不进入用户库解析路径。
    """

    local: SkillLocalRepository
    branches: SnapshotBranches

    async def prepare(self, account: ToolAccount, library_names: set[str]) -> list[PreparedSkill]:
        """
        本地与库来源同名时显式拒绝，不能把路径相等当作身份相等。

        :param account (ToolAccount): 已授权账户
        :param library_names (set[str]): 本次已选库来源名称
        :return list[PreparedSkill]: 已发布且启用的精确本地分支
        """
        items = await self.local.active(account.user_id, account.id)
        if any(item.name in library_names for item in items):
            raise SkillContentError(
                "SKILL_SOURCE_CONFLICT", "local and library skill names collide"
            )
        selected = []
        for item in items:
            assert item.default_revision_id is not None
            branch = await self.local.branch(
                account.user_id, account.id, item.id, item.default_revision_id
            )
            if branch is not None and branch.expired:
                raise SkillContentError(
                    "STATE_EXPIRED", "selected branch requires explicit reset or restore"
                )
            if branch is None:
                if await self.local.has_previous_branch(account.user_id, account.id, item.id):
                    raise SkillContentError(
                        "STATE_MIGRATION_REQUIRED", "local revision requires state migration"
                    )
                branch = AccountSkillState(
                    id=uuid4(),
                    user_id=account.user_id,
                    account_id=account.id,
                    local_skill_id=item.id,
                    local_revision_id=item.default_revision_id,
                    installation_epoch=1,
                    epoch=1,
                    expired=False,
                )
                self.branches.runtime.add(branch)
                await self.branches.runtime.flush()
            if branch.head_checkpoint_id is None:
                checkpoint = await self.initialize(item, branch)
            else:
                checkpoint = await self.branches.require_checkpoint(
                    account.user_id, account.id, branch.head_checkpoint_id
                )
            if checkpoint.scope != "item" or checkpoint.state_id != branch.id:
                raise SkillContentError("STATE_SCOPE_MISMATCH", "local branch head is inconsistent")
            assert checkpoint.tree_digest is not None
            tree = await self.branches.content.read_tree(
                account.user_id, "state", checkpoint.tree_digest
            )
            selected.append(
                PreparedSkill(
                    item.name,
                    ResolvedSkillRule(
                        enabled=True,
                        revision_id=item.default_revision_id,
                        enabled_source="account",
                        revision_source="account",
                        eligible=True,
                        included=True,
                        exclusion_reason=None,
                    ),
                    branch,
                    checkpoint,
                    SkillSubtree(item.name, checkpoint.subtree_prefix, tree),
                )
            )
        return selected

    async def initialize(
        self, item: AccountLocalSkill, branch: AccountSkillState
    ) -> SkillCheckpoint:
        """
        从同账户完整原始树建立分支视图，不抽取会破坏链接的独立包。

        :param item (AccountLocalSkill): 已授权本地来源
        :param branch (AccountSkillState): 未初始化的同源运行分支
        :return SkillCheckpoint: 保留完整依赖的初始子树视图
        """
        assert item.default_revision_id is not None
        revision = await self.local.revision(
            item.user_id, item.account_id, item.id, item.default_revision_id
        )
        if revision is None or not revision.retained or revision.tree_digest is None:
            raise SkillContentError("REVISION_EXPIRED", "local initial state is no longer retained")
        await self.branches.content.read_tree(item.user_id, "state", revision.tree_digest)
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=item.user_id,
            account_id=item.account_id,
            scope="item",
            state_id=branch.id,
            state_epoch=branch.epoch,
            backing_directory_id=item.source_checkpoint_id if revision.number == 1 else None,
            subtree_prefix=revision.subtree_prefix,
            content_digest=revision.tree_digest,
            tree_digest=revision.tree_digest,
        )
        self.branches.runtime.add(checkpoint)
        await self.branches.runtime.flush()
        branch.head_checkpoint_id = checkpoint.id
        await self.branches.runtime.flush()
        return checkpoint
