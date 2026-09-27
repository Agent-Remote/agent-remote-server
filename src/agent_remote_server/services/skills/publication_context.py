"""
在用户锁内准备发布的精确分支与完整当前比较树。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
)
from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService


@dataclass(frozen=True)
class PublicationBranch:
    """
    同一锁内读取的快照身份、当前运行分支以及本次写入标志。
    """

    item: SessionSkillSnapshotItem
    state: AccountSkillState
    checkpoint: SkillCheckpoint | None
    changed: bool


@dataclass
class PublicationContext:
    """
    共享已锁定发布事务的仓储与私有内容读取。
    """

    library: SkillLibraryRepository
    repository: SkillPublicationRepository
    runtime: SkillRuntimeRepository
    content: SkillContentService

    async def branches(
        self,
        snapshot: SessionSkillSnapshot,
        base: SkillTreeManifest,
        incoming: SkillTreeManifest,
    ) -> tuple[list[PublicationBranch], str | None]:
        """
        只对真正发生变化的原始分支检查 epoch 和来源有效性。

        :param snapshot (SessionSkillSnapshot): 原始精确快照
        :param base (SkillTreeManifest): 当时实际暴露的完整目录
        :param incoming (SkillTreeManifest): 已持久化完整会话输入
        :return tuple[list[PublicationBranch], str | None]: 分支视图及整个提交的归档原因
        """
        selected: list[PublicationBranch] = []
        reason = None
        for item in await self.runtime.snapshot_items(snapshot):
            state = await self.repository.branch(
                snapshot.user_id, snapshot.account_id, item.state_id
            )
            changed = subtree_entries(base, item.entry_name) != subtree_entries(
                incoming, item.entry_name
            )
            checkpoint = None
            if state.head_checkpoint_id is not None and not state.expired:
                checkpoint = await self.runtime.checkpoint(
                    snapshot.user_id, snapshot.account_id, state.head_checkpoint_id
                )
                if checkpoint is not None and (
                    not checkpoint.retained or checkpoint.tree_digest is None
                ):
                    checkpoint = None
            source_reason = await self.source_invalid(state)
            if changed:
                revision = state.base_revision_id or state.local_revision_id
                if str(revision) != item.resolution_json.get("revision_id"):
                    reason = reason or "target_revision_changed"
                elif state.epoch != item.state_epoch:
                    reason = reason or "state_epoch_changed"
                elif state.expired or checkpoint is None:
                    reason = reason or "state_expired"
                else:
                    reason = reason or source_reason
            elif source_reason is not None:
                checkpoint = None
            selected.append(PublicationBranch(item, state, checkpoint, changed))
        return selected, reason

    async def source_invalid(self, state: AccountSkillState) -> str | None:
        """
        默认 revision 漂移不影响旧分支，但移除或重装来源不能接受旧写入。

        :param state (AccountSkillState): 精确旧分支
        :return str | None: 失效原因或仍有效
        """
        if state.installation_id is not None:
            installation = await self.library.installation(
                state.user_id, str(state.installation_id)
            )
            if installation is None or installation.removed:
                return "source_removed"
            if installation.epoch != state.installation_epoch:
                return "installation_epoch_changed"
        else:
            assert state.local_skill_id is not None
            local = await self.repository.local(
                state.user_id, state.account_id, state.local_skill_id
            )
            if local is None or local.status != "active":
                return "source_removed"
        return None

    async def current_tree(
        self,
        user_id: UUID,
        directory: SkillTreeManifest,
        selected: list[PublicationBranch],
    ) -> tuple[SkillTreeManifest, bool]:
        """
        当前目录保留未暴露条目，再覆盖可用的原始分支 head。

        :param user_id (UUID): 用户身份
        :param directory (SkillTreeManifest): 当前权威目录完整树
        :param selected (list[PublicationBranch]): 原快照暴露的精确分支
        :return tuple[SkillTreeManifest, bool]: 当前比较树及是否遇到不可组合依赖
        """
        entries = {entry.path: entry for entry in directory.entries}
        for branch in selected:
            checkpoint = branch.checkpoint
            if checkpoint is None:
                continue
            assert checkpoint.tree_digest is not None
            if checkpoint.subtree_prefix != branch.item.entry_name:
                raise SkillContentError("STATE_SCOPE_MISMATCH", "branch changed its name")
            tree = await self.content.read_tree(user_id, "state", checkpoint.tree_digest)
            name = branch.item.entry_name
            entries = {
                path: entry
                for path, entry in entries.items()
                if path != name and not path.startswith(name + "/")
            }
            entries.update((entry.path, entry) for entry in subtree_entries(tree, name))
        try:
            return SkillTreeManifest(
                entries=tuple(sorted(entries.values(), key=lambda entry: entry.path.encode()))
            ), False
        except ValueError:
            # 比较投影不可组合时仍保留实际完整目录；各分支 head 另有外键引用供恢复。
            return directory, True


def subtree_entries(tree: SkillTreeManifest, name: str) -> tuple[SkillTreeEntry, ...]:
    """
    只比较一个显式身份的命名空间，不把未暴露目录视作删除。

    :param tree (SkillTreeManifest): 完整目录树
    :param name (str): 已确定身份的顶层名称
    :return tuple[SkillTreeEntry, ...]: 保留原前缀的全部条目
    """
    return tuple(
        entry for entry in tree.entries if entry.path == name or entry.path.startswith(name + "/")
    )


def directory_members(
    current: list[SkillDirectoryMember],
    branches: list[PublicationBranch],
) -> dict[str, tuple[UUID, UUID]]:
    """
    未暴露成员保持原引用，已暴露成员使用本次精确分支的当前 head。

    :param current (list[SkillDirectoryMember]): 当前目录成员
    :param branches (list[PublicationBranch]): 精确分支视图
    :return dict[str, tuple[UUID, UUID]]: 名称到分支与检查点的引用
    """
    members = {item.entry_name: (item.state_id, item.checkpoint_id) for item in current}
    for branch in branches:
        if branch.checkpoint is not None:
            members[branch.item.entry_name] = (branch.state.id, branch.checkpoint.id)
    return members
