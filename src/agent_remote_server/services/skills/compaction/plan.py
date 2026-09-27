"""
保存目录整理的完整不可变比较计划，区分内容变化、成员替换和无法移除的链接依赖。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.directory_merge import directory_merge_units


@dataclass(frozen=True, order=True)
class CompactionMember:
    """
    目录中的精确成员身份，不能用同名新版或另一纪元替代。
    """

    entry_name: str
    state_id: UUID
    checkpoint_id: UUID


@dataclass(frozen=True)
class CompactionHead:
    """
    固定受保护分支的完整旧视图，缺少 backing 的旧证据只能保持原样。
    """

    state_id: UUID
    checkpoint_id: UUID
    state_epoch: int
    subtree_prefix: str
    tree_digest: str
    backing_directory_id: UUID | None
    invalid_skill_format: bool


@dataclass(frozen=True)
class CompactionDirectory:
    """
    一份完整目录的规范结果；未选根与关联单元保留，不把文件存在误当成可回收性。
    """

    checkpoint_id: UUID
    directory_epoch: int | None
    tree_digest: str
    members: tuple[CompactionMember, ...]
    original: SkillTreeManifest
    result: SkillTreeManifest
    removed: tuple[CompactionMember, ...]
    blocked: tuple[CompactionMember, ...]
    invalid_skill_format: bool


@dataclass(frozen=True)
class CompactionPlan:
    """
    只读计划供内部原子发布精确比较，不是公开 prune 回执或删除授权。
    """

    user_id: UUID
    account_id: UUID
    checkpoint_ids: tuple[UUID, ...]
    all_unreferenced: bool
    library_generation: int
    directory_head_id: UUID
    directory_epoch: int
    heads: tuple[CompactionHead, ...]
    directories: tuple[CompactionDirectory, ...]

    def changed_directories(self) -> frozenset[UUID]:
        """
        成员替换传播到持有这些 head 的其他目录，直到全部当前物化引用同时更新。

        :return frozenset[UUID]: 需要新建等价目录身份的有限闭包
        """
        changed = {
            directory.checkpoint_id
            for directory in self.directories
            if directory.original != directory.result
        }
        consumers: dict[UUID, set[UUID]] = {}
        for directory in self.directories:
            for member in directory.members:
                consumers.setdefault(member.checkpoint_id, set()).add(directory.checkpoint_id)
        heads: dict[UUID, list[UUID]] = {}
        for head in self.heads:
            if head.backing_directory_id is not None:
                heads.setdefault(head.backing_directory_id, []).append(head.checkpoint_id)
        pending = list(changed)
        while pending:
            directory_id = pending.pop()
            for checkpoint_id in heads.get(directory_id, ()):
                for consumer in consumers.get(checkpoint_id, ()):
                    if consumer not in changed:
                        changed.add(consumer)
                        pending.append(consumer)
        return frozenset(changed)


@dataclass(frozen=True)
class CompactionResult:
    """
    返回本次实际发布身份，旧历史是否到期与配额结算仍由后续保留流程决定。
    """

    directory_head_id: UUID
    directory_replacements: tuple[tuple[UUID, UUID], ...]
    head_replacements: tuple[tuple[UUID, UUID], ...]


type CompactedTree = tuple[
    SkillTreeManifest, tuple[CompactionMember, ...], tuple[CompactionMember, ...]
]


def compact_tree(
    tree: SkillTreeManifest,
    members: tuple[CompactionMember, ...],
    selected: frozenset[UUID],
    preserved_roots: frozenset[str],
) -> CompactedTree:
    """
    只移除完整可分离的已选根，保留跨根导出依赖单元的全部文件和链接语义。

    :param tree (SkillTreeManifest): 已验证的完整原目录
    :param members (tuple[CompactionMember, ...]): 该目录原始精确成员
    :param selected (frozenset[UUID]): 已授权并通过期限校验的 checkpoint 身份
    :param preserved_roots (frozenset[str]): 该完整树上仍受保护的单项视图根
    :return CompactedTree: 完整结果、实际移除和依赖阻断成员
    """
    candidates = {member.entry_name for member in members if member.checkpoint_id in selected}
    roots = {entry.path.split("/", 1)[0] for entry in tree.entries}
    removable: set[str] = set()
    for unit in directory_merge_units((tree,), roots):
        if set(unit) <= candidates and not (set(unit) & preserved_roots):
            removable.update(unit)
    result = SkillTreeManifest(
        entries=tuple(
            entry for entry in tree.entries if entry.path.split("/", 1)[0] not in removable
        )
    )
    removed = tuple(member for member in members if member.entry_name in removable)
    blocked = tuple(
        member
        for member in members
        if member.entry_name in candidates and member.entry_name not in removable
    )
    return result, removed, blocked
