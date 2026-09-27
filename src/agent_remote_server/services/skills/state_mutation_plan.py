"""
构建重置恢复的完整目录结果，验证来源集合、跨项依赖与实际字节。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_state import SkillCheckpoint, SkillDirectoryMember
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_state_commands import (
    SkillCurrentStateView,
    SkillStateCommand,
    SkillStateTarget,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    validate_finalization_limits,
)
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@dataclass
class StateMutationPlan:
    """
    尚未创建任何新内容引用的完整待发布结果。
    """

    previous: SkillCheckpoint
    current: SkillTreeManifest
    result: SkillTreeManifest
    preserved: list[SkillDirectoryMember]


@dataclass
class StateMutationPlanner:
    """
    所有实际内容校验都发生在预览与提交共用的路径。
    """

    queries: SkillStateQueryService
    local: SkillLocalRepository
    store: PrivateObjectStore
    policy: SkillStoragePolicy

    async def prepare(
        self, user_id: UUID, request: SkillStateCommand, current: SkillCurrentStateView
    ) -> StateMutationPlan:
        """
        重置读取原始包，恢复读取授权历史，保留未选中成员而不默默修复依赖。

        :param user_id (UUID): 当前用户
        :param request (SkillStateCommand): 已核对前置条件的命令
        :param current (SkillCurrentStateView): 已锁定当前有效选择
        :return StateMutationPlan: 完整验证后的目录变化
        """
        head_id = current.precondition.directory_head_id
        if current.precondition.directory_mode != "managed_v1" or head_id is None:
            raise SkillContentError(
                "STATE_NOT_MANAGED", "account must complete managed takeover first"
            )
        previous = await self.queries.require(user_id, head_id)
        tree = await self._tree(previous)
        targets = current.precondition.targets
        selected = {item.name for item in targets}
        for target in targets:
            library = await self.queries.library.installation(user_id, target.name)
            local = await self.queries.repository.local_source(
                user_id, request.selector.account_id, target.name
            )
            if any(item is not None and item.id != target.skill_id for item in (library, local)):
                raise SkillContentError(
                    "SKILL_SOURCE_CONFLICT", "another active source occupies the selected name"
                )
        members = list(await self.queries.runtime.members(previous))
        preserved = [member for member in members if member.entry_name not in selected]
        entries: list[SkillTreeEntry] = []
        if request.action == "restore":
            assert request.checkpoint_id is not None
            source = await self.queries.require(user_id, request.checkpoint_id)
            await self._restore_scope(source, request, targets)
            replacement = await self._tree(source)
        else:
            entries = []
            for target in targets:
                entries.extend(await self._original(user_id, request.selector.account_id, target))
            replacement = None
        if request.selector.scope == "item":
            name = targets[0].name
            retained = [entry for entry in tree.entries if not _within(entry.path, name)]
            added = (
                entries
                if replacement is None
                else [entry for entry in replacement.entries if _within(entry.path, name)]
            )
        else:
            hidden = {member.entry_name for member in preserved}
            retained = [entry for entry in tree.entries if entry.path.split("/", 1)[0] in hidden]
            if replacement is not None and any(
                entry.path.split("/", 1)[0] in hidden for entry in replacement.entries
            ):
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "restore includes data reserved by unselected sources"
                )
            added = entries if replacement is None else list(replacement.entries)
        try:
            result = SkillTreeManifest(
                entries=tuple(sorted(retained + added, key=lambda entry: entry.path.encode()))
            )
        except ValueError as error:
            raise SkillContentError(
                "STATE_DEPENDENCY_MISSING", "state change has incompatible or missing dependencies"
            ) from error
        validate_finalization_limits(
            result, selected | {member.entry_name for member in preserved}, self.policy
        )
        try:
            await self.store.verify_manifest(user_id, result)
        except FileNotFoundError as error:
            raise SkillContentError("CONTENT_INCOMPLETE", "state content is unavailable") from error
        except ValueError as error:
            raise SkillContentError(
                "CONTENT_INVALID", "state content verification failed"
            ) from error
        await self.queries.content.validate_state_admission(user_id, result)
        return StateMutationPlan(previous, tree, result, preserved)

    async def _tree(self, checkpoint: SkillCheckpoint) -> SkillTreeManifest:
        """
        恢复仅接受完整保留内容，墓碑不能变为空快照。

        :param checkpoint (SkillCheckpoint): 已授权检查点
        :return SkillTreeManifest: 完整原始树
        """
        if not checkpoint.retained or checkpoint.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "checkpoint is no longer retained")
        return await self.queries.content.read_tree(
            checkpoint.user_id, "state", checkpoint.tree_digest
        )

    async def _restore_scope(
        self,
        source: SkillCheckpoint,
        request: SkillStateCommand,
        targets: tuple[SkillStateTarget, ...],
    ) -> None:
        """
        允许同稳定来源的旧安装纪元，禁止跨账户、名称、来源或选定版本恢复。

        :param source (SkillCheckpoint): 已授权恢复输入
        :param request (SkillStateCommand): 明确命令
        :param targets (tuple[SkillStateTarget, ...]): 当前选中来源集合
        """
        scope = "item" if request.selector.scope == "item" else "directory"
        if source.account_id != request.selector.account_id or source.scope != scope:
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "checkpoint has another account or scope"
            )
        expected = {(item.name, item.skill_id, item.revision_id) for item in targets}
        actual = set()
        if scope == "item":
            branch = await self.queries.repository.branch(source)
            assert branch is not None
            actual.add(
                (
                    source.subtree_prefix,
                    branch.installation_id or branch.local_skill_id,
                    branch.base_revision_id or branch.local_revision_id,
                )
            )
        else:
            for member in await self.queries.runtime.members(source):
                checkpoint = await self.queries.require(source.user_id, member.checkpoint_id)
                branch = await self.queries.repository.branch(checkpoint)
                assert branch is not None
                actual.add(
                    (
                        member.entry_name,
                        branch.installation_id or branch.local_skill_id,
                        branch.base_revision_id or branch.local_revision_id,
                    )
                )
        if expected != actual:
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH",
                "restore identities do not match current effective sources",
                details={
                    "expected": sorted(
                        (name, str(skill), str(revision)) for name, skill, revision in expected
                    ),
                    "actual": sorted(
                        (name, str(skill), str(revision)) for name, skill, revision in actual
                    ),
                },
            )

    async def _original(
        self, user_id: UUID, account_id: UUID, target: SkillStateTarget
    ) -> list[SkillTreeEntry]:
        """
        原始包与本地初始完整树分别授权，只选取声明的子树而不借用旧辅助数据。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已授权账户
        :param target (SkillStateTarget): 当前有效版本
        :return list[SkillTreeEntry]: 带账户前缀的精确原始条目
        """
        if target.origin == "user_library":
            revision = await self.queries.library.revision(
                user_id, target.skill_id, str(target.revision_id)
            )
            if revision is None or not revision.retained or revision.tree_digest is None:
                raise SkillContentError(
                    "REVISION_EXPIRED", "original package is no longer retained"
                )
            tree = await self.queries.content.read_tree(user_id, "package", revision.tree_digest)
            return [
                SkillTreeEntry(path=target.name, kind="directory", mode=0o755),
                *(
                    entry.model_copy(update={"path": target.name + "/" + entry.path})
                    for entry in tree.entries
                ),
            ]
        local = await self.local.revision(user_id, account_id, target.skill_id, target.revision_id)
        if local is None or not local.retained or local.tree_digest is None:
            raise SkillContentError("REVISION_EXPIRED", "local initial state is no longer retained")
        if local.subtree_prefix != target.name:
            raise SkillContentError("STATE_SCOPE_MISMATCH", "local initial name has changed")
        tree = await self.queries.content.read_tree(user_id, "state", local.tree_digest)
        return [entry for entry in tree.entries if _within(entry.path, target.name)]


def _within(path: str, name: str) -> bool:
    """
    以真实路径组件边界确定成员范围。

    :param path (str): 相对路径
    :param name (str): 顶层成员名
    :return bool: 是否属于该成员
    """
    return path == name or path.startswith(name + "/")
