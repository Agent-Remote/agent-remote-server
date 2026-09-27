"""
有界计算语义保活闭包，历史外键不自动成为永久根。
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

type RetentionKind = Literal[
    "operation",
    "directory_context",
    "branch",
    "checkpoint",
    "revision",
    "local_revision",
    "snapshot",
    "finalization",
    "publication",
    "migration",
    "migration_baseline",
    "takeover",
    "upload",
    "package_tree",
    "state_tree",
    "package_object",
    "state_object",
    "blob",
]
type ProtectionReason = Literal[
    "pending_operation",
    "library_default",
    "pin",
    "current_branch",
    "local_original",
    "active_snapshot",
    "pending_finalization",
    "publication_conflict",
    "migration_conflict",
    "pending_takeover",
    "upload_lease",
    "current_directory",
]


@dataclass(frozen=True, order=True)
class RetentionKey:
    """
    单用户分析中的逻辑对象身份，额度分类与底层文件分别建模。
    """

    kind: RetentionKind
    identity: str


@dataclass(frozen=True)
class DirectoryReference:
    """
    当前目录中的物化成员，退役前需要显式整理而非忽略该引用。
    """

    account_id: UUID
    directory_checkpoint_id: UUID
    entry_name: str
    state_id: UUID
    checkpoint_id: UUID


@dataclass(frozen=True)
class RetentionProtection:
    """
    保护闭包只说明不可删除对象，不授予其他对象删除资格。
    """

    roots: dict[RetentionKey, frozenset[ProtectionReason]]
    protected: dict[RetentionKey, frozenset[ProtectionReason]]
    directory_members: tuple[DirectoryReference, ...]

    def reasons(self, kind: RetentionKind, identity: UUID | str) -> frozenset[ProtectionReason]:
        """
        读取对象所有传播到达的保护理由。

        :param kind (RetentionKind): 精确对象种类
        :param identity (UUID | str): 稳定身份或摘要
        :return frozenset[ProtectionReason]: 保护理由，空值仍不代表可删除
        """
        return self.protected.get(RetentionKey(kind, str(identity)), frozenset())


@dataclass
class RetentionGraph:
    """
    用理由增量传播处理共享输入和环，限制内存与解释规模。
    """

    max_nodes: int = 1_000_000
    max_edges: int = 4_000_000
    _edges: dict[RetentionKey, set[RetentionKey]] = field(default_factory=dict)
    _roots: dict[RetentionKey, set[ProtectionReason]] = field(default_factory=dict)
    _edge_count: int = 0
    _failed: bool = False

    def _node(self, kind: RetentionKind, identity: UUID | str) -> RetentionKey:
        """
        登记有界对象集合，超限时拒绝不完整分析。

        :param kind (RetentionKind): 对象种类
        :param identity (UUID | str): 原始身份
        :return RetentionKey: 图中规范身份
        """
        key = RetentionKey(kind, str(identity))
        if not key.identity:
            self._failed = True
            raise ValueError("empty retention identity")
        if key not in self._edges:
            if len(self._edges) >= self.max_nodes:
                self._failed = True
                raise ValueError("retention graph node limit exceeded")
            self._edges[key] = set()
        return key

    def root(self, kind: RetentionKind, identity: UUID | str, reason: ProtectionReason) -> None:
        """
        只有明确的业务根才能开始传播。

        :param kind (RetentionKind): 根对象种类
        :param identity (UUID | str): 根身份
        :param reason (ProtectionReason): 业务保护理由
        """
        self._roots.setdefault(self._node(kind, identity), set()).add(reason)

    def edge(
        self,
        source_kind: RetentionKind,
        source: UUID | str,
        target_kind: RetentionKind,
        target: UUID | str | None,
    ) -> None:
        """
        添加方向明确的内容依赖，缺失可选引用不构造虚假节点。

        :param source_kind (RetentionKind): 引用方种类
        :param source (UUID | str): 引用方身份
        :param target_kind (RetentionKind): 被引用对象种类
        :param target (UUID | str | None): 可选依赖身份
        """
        if target is None:
            return
        origin = self._node(source_kind, source)
        destination = self._node(target_kind, target)
        if destination not in self._edges[origin]:
            if self._edge_count >= self.max_edges:
                self._failed = True
                raise ValueError("retention graph edge limit exceeded")
            self._edges[origin].add(destination)
            self._edge_count += 1

    def protect(self, members: tuple[DirectoryReference, ...] = ()) -> RetentionProtection:
        """
        每个对象的每种理由只传播一次，结果与插入顺序无关。

        :param members (tuple[DirectoryReference, ...]): 当前目录物化义务
        :return RetentionProtection: 完整保护闭包和独立目录引用
        """
        if self._failed:
            raise ValueError("retention graph is incomplete")
        reached = {key: set(reasons) for key, reasons in self._roots.items()}
        pending = deque((key, reason) for key, reasons in reached.items() for reason in reasons)
        while pending:
            origin, reason = pending.popleft()
            for target in self._edges[origin]:
                reasons = reached.setdefault(target, set())
                if reason not in reasons:
                    reasons.add(reason)
                    pending.append((target, reason))
        return RetentionProtection(
            roots={key: frozenset(reasons) for key, reasons in self._roots.items()},
            protected={key: frozenset(reasons) for key, reasons in reached.items()},
            directory_members=members,
        )
