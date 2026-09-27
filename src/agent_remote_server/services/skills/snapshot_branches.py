"""
在用户存储锁内解析有效分支，并为首次使用版本准备独立初始 checkpoint。
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule, SkillRuleOverride
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.skill_manager.materialization import SkillSubtree, compose_directory
from agent_remote_server.skill_manager.rules import resolve_skill_rule
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass(frozen=True)
class PreparedSkill:
    """
    规则、分支和起始 checkpoint 始终使用同一个已锁定配置视图。
    """

    name: str
    rule: ResolvedSkillRule
    state: AccountSkillState
    checkpoint: SkillCheckpoint
    subtree: SkillSubtree


@dataclass
class SnapshotBranches:
    """
    共享外层事务的分支准备依赖。
    """

    library: SkillLibraryRepository
    runtime: SkillRuntimeRepository
    content: SkillContentService
    policy: SkillStoragePolicy

    async def prepare(self, account: ToolAccount) -> list[PreparedSkill]:
        """
        解析当前账户全部有效用户库条目，不从同名目录推断来源。

        :param account (ToolAccount): 已授权账户
        :return list[PreparedSkill]: 本次实际暴露的独立分支
        """
        selected = []
        for item in await self.library.list_installations(account.user_id):
            assert item.default_revision_id is not None
            tools, accounts = await self.library.rules(item)
            tool_rule = next(
                (
                    SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
                    for row in tools
                    if row.tool_type == account.tool_type
                ),
                None,
            )
            account_rule = next(
                (
                    SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
                    for row in accounts
                    if row.account_id == account.id
                ),
                None,
            )
            rule = resolve_skill_rule(
                item.default_enabled, item.default_revision_id, tool_rule, account_rule
            )
            if not rule.included:
                continue
            branch = await self.runtime.branch(
                account.user_id, account.id, item.id, item.epoch, rule.revision_id
            )
            if branch is not None and branch.expired:
                raise SkillContentError(
                    "STATE_EXPIRED", "selected branch requires explicit reset or restore"
                )
            if (
                branch is None or branch.head_checkpoint_id is None
            ) and await self.runtime.has_previous_branch(
                account.user_id, account.id, item.id, item.epoch
            ):
                raise SkillContentError(
                    "STATE_MIGRATION_REQUIRED",
                    "selected revision requires state migration before preparation",
                )
            if branch is None:
                branch = AccountSkillState(
                    id=uuid4(),
                    user_id=account.user_id,
                    account_id=account.id,
                    installation_id=item.id,
                    installation_epoch=item.epoch,
                    base_revision_id=rule.revision_id,
                    epoch=1,
                    expired=False,
                )
                self.runtime.add(branch)
                await self.runtime.flush()
            if branch.head_checkpoint_id is None:
                checkpoint = await self.initialize(branch, item.name)
            else:
                checkpoint = await self.require_checkpoint(
                    account.user_id, account.id, branch.head_checkpoint_id
                )
            if checkpoint.scope != "item" or checkpoint.state_id != branch.id:
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "branch head does not match selected state"
                )
            assert checkpoint.tree_digest is not None
            tree = await self.content.read_tree(account.user_id, "state", checkpoint.tree_digest)
            selected.append(
                PreparedSkill(
                    item.name,
                    rule,
                    branch,
                    checkpoint,
                    SkillSubtree(item.name, checkpoint.subtree_prefix, tree),
                )
            )
        return selected

    async def require_checkpoint(
        self, user_id: UUID, account_id: UUID, checkpoint_id: UUID
    ) -> SkillCheckpoint:
        """
        过期内容不允许被隐式重置为原始包。

        :param user_id (UUID): 用户身份
        :param account_id (UUID): 账户身份
        :param checkpoint_id (UUID): 预期检查点
        :return SkillCheckpoint: 已保留的完整检查点
        """
        checkpoint = await self.runtime.checkpoint(user_id, account_id, checkpoint_id)
        if checkpoint is None or not checkpoint.retained or checkpoint.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "checkpoint is no longer retained")
        return checkpoint

    async def initialize(self, branch: AccountSkillState, name: str) -> SkillCheckpoint:
        """
        用不可变原始包建立该账户该版本的初始独立状态引用。

        :param branch (AccountSkillState): 未初始化的已授权分支
        :param name (str): 当前来源名称
        :return SkillCheckpoint: 新建初始子树检查点
        """
        if branch.installation_id is None or branch.base_revision_id is None:
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "initial branch is not a library source"
            )
        revision = await self.library.revision(
            branch.user_id, branch.installation_id, str(branch.base_revision_id)
        )
        if revision is None or not revision.retained or revision.tree_digest is None:
            raise SkillContentError("REVISION_EXPIRED", "initial package is no longer retained")
        package = await self.content.read_tree(branch.user_id, "package", revision.tree_digest)
        tree = compose_directory(
            SkillTreeManifest(),
            set(),
            [SkillSubtree(name, "", package)],
            self.policy.checkpoint_bytes,
        )
        upload = await self.content.begin(branch.user_id, f"branch:{branch.id}", tree, "state")
        stored = await self.content.complete(branch.user_id, upload.id)
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=branch.user_id,
            account_id=branch.account_id,
            scope="item",
            state_id=branch.id,
            state_epoch=branch.epoch,
            subtree_prefix=name,
            content_digest=stored.digest,
            tree_digest=stored.digest,
        )
        self.runtime.add(checkpoint)
        await self.runtime.flush()
        branch.head_checkpoint_id = checkpoint.id
        await self.runtime.flush()
        return checkpoint
