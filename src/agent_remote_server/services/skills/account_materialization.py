"""
为会话与后台部署共用完整目录组装，不记录会话或实际使用。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.services.skills.snapshot_branches import PreparedSkill, SnapshotBranches
from agent_remote_server.services.skills.snapshot_local import SnapshotLocalBranches
from agent_remote_server.services.tool_registry import ToolRegistry
from agent_remote_server.skill_manager.materialization import (
    MaterializationError,
    compose_directory,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass(frozen=True)
class PreparedAccountDirectory:
    """
    同一用户锁内组装的完整树、目录纪元与精确分支输入。
    """

    epoch: int
    head: SkillCheckpoint
    selected: tuple[PreparedSkill, ...]
    manifest: SkillTreeManifest


class AccountMaterialization:
    """
    调用方负责能力检查、迁移准备及包围整个保存过程的用户锁和保存点。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共用账户准备依赖，持久化用途由上层显式决定。

        :param session (AsyncSession): 已锁定用户事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 内容配额
        """
        self.runtime = SkillRuntimeRepository(session)
        self.content = SkillContentService(session, store, policy)
        self.branches = SnapshotBranches(
            SkillLibraryRepository(session), self.runtime, self.content, policy
        )
        self.local = SnapshotLocalBranches(SkillLocalRepository(session), self.branches)
        self.store = store
        self.policy = policy

    async def prepare(self, account: ToolAccount) -> PreparedAccountDirectory:
        """
        完整保留根级数据并替换精确选中条目，格式错误不降格为匿名内容。

        :param account (ToolAccount): 已授权账户
        :return PreparedAccountDirectory: 尚未保存为部署或会话输入的完整目录
        """
        if account.tool_type not in ToolRegistry.supported_tool_types():
            raise SkillContentError("UNSUPPORTED_TOOL", "tool adapter is not registered")
        directory = await self.runtime.directory(account.user_id, account.id)
        if (
            directory is None
            or directory.mode != "managed_v1"
            or directory.head_checkpoint_id is None
        ):
            raise SkillContentError(
                "MIGRATION_PENDING", "account directory takeover is not complete"
            )
        head = await self.branches.require_checkpoint(
            account.user_id, account.id, directory.head_checkpoint_id
        )
        if head.scope != "directory":
            raise SkillContentError("STATE_SCOPE_MISMATCH", "account head is not a directory")
        assert head.tree_digest is not None
        tree = await self.content.read_tree(account.user_id, "state", head.tree_digest)
        members = await self.runtime.members(head)
        try:
            selected = await self.branches.prepare(account)
            selected.extend(await self.local.prepare(account, {item.name for item in selected}))
            materialized = compose_directory(
                tree,
                {member.entry_name for member in members},
                [item.subtree for item in selected],
                self.policy.checkpoint_bytes,
            )
        except MaterializationError as error:
            raise SkillContentError(error.code, str(error)) from error
        for item in selected:
            entry = _skill_document(materialized, item.name + "/SKILL.md")
            _metadata(await self.store.read_prefix(account.user_id, entry, 65_544), item.name)
        return PreparedAccountDirectory(directory.epoch, head, tuple(selected), materialized)
