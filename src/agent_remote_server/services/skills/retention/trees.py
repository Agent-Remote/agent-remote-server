"""
解释完整树的实际等待与全部保留历史外键，不把没有硬保护当作允许删除。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.repositories.skill_retention_schema import TREE_CONTENT_COLUMNS
from agent_remote_server.skill_manager.retention.graph import (
    ProtectionReason,
    RetentionKey,
    RetentionKind,
    RetentionProtection,
)
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy

_REFERENCE_HISTORIES: dict[str, RetentionKind] = {
    "skill_revisions": "revision",
    "account_local_skill_revisions": "local_revision",
    "skill_checkpoints": "checkpoint",
    "session_skill_snapshots": "snapshot",
    "skill_finalizations": "finalization",
    "skill_publications": "publication",
    "skill_branch_preparations": "migration",
    "skill_resolution_choices": "publication",
    "skill_migration_resolution_content": "migration",
}


@dataclass(frozen=True, order=True)
class StoredTreeReference:
    """
    一项仍在数据库中生效的历史内容外键，不因其消费者无硬保护而忽略。
    """

    table: str
    identity: tuple[str, ...]
    column: str
    account_id: UUID | None

    @property
    def history_key(self) -> RetentionKey:
        """
        复合授权的内容生命周期由首项原比较身份控制，未分类引用不能被投影移除。

        :return RetentionKey: 对应本引用的精确历史消费者
        """
        if self.table not in _REFERENCE_HISTORIES or not self.identity:
            raise ValueError("unclassified tree reference history")
        return RetentionKey(_REFERENCE_HISTORIES[self.table], self.identity[0])


@dataclass(frozen=True)
class StoredTreeRetention:
    """
    同用户完整树的等待与实际引用，物理文件和分类配额仍须独立核算。
    """

    key: RetentionKey
    reasons: frozenset[ProtectionReason]
    released_at: datetime | None
    expires_at: datetime | None
    references: tuple[StoredTreeReference, ...]


def tree_retention(
    index: RetentionIndex, protected: RetentionProtection, policy: SkillStoragePolicy
) -> tuple[StoredTreeRetention, ...]:
    """
    完整树使用普通等待，归档历史自身的更长期限和外键仍独立阻断内容退役。

    :param index (RetentionIndex): 用户锁内完整引用索引
    :param protected (RetentionProtection): 同事务真实保护闭包
    :param policy (SkillStoragePolicy): 当前部署保留策略
    :return tuple[StoredTreeRetention, ...]: 稳定排序的时钟和真实内容引用，不是删除授权
    """
    references = tree_references(index)
    result = []
    for tree in index.trees:
        key = RetentionKey(
            "package_tree" if tree.category == "package" else "state_tree", tree.digest
        )
        reasons = protected.protected.get(key, frozenset())
        released = tree.retention_released_at
        if released is not None and released.tzinfo is None:
            released = released.replace(tzinfo=UTC)
        result.append(
            StoredTreeRetention(
                key,
                reasons,
                released,
                released + timedelta(days=policy.history_days)
                if released is not None and not reasons
                else None,
                references.get(key, ()),
            )
        )
    return tuple(sorted(result, key=lambda row: row.key))


def tree_references(index: RetentionIndex) -> dict[RetentionKey, tuple[StoredTreeReference, ...]]:
    """
    逐一对应已登记的真实入向外键，包括生成保留摘要和复合授权主键。

    :param index (RetentionIndex): 已授权完整索引
    :return dict[RetentionKey, tuple[StoredTreeReference, ...]]: 分类/摘要到全部实际历史引用
    """
    result: dict[RetentionKey, list[StoredTreeReference]] = {}

    def add(
        table: str,
        identity: tuple[str, ...],
        account_id: UUID | None,
        category: str,
        digest: str | None,
        column: str,
    ) -> None:
        """
        空生成摘要表示已解除内容引用；未知字段整体拒绝而非漏报。

        :param table (str): 持久化来源表
        :param identity (tuple[str, ...]): 完整主键身份
        :param account_id (UUID | None): 账户历史归属，原始包无账户范围
        :param category (str): 独立计量分类
        :param digest (str | None): 生效的实际内容外键值
        :param column (str): 生效内容列
        """
        if (table, column) not in TREE_CONTENT_COLUMNS:
            raise ValueError("unclassified skill tree content reference")
        if digest is None:
            return
        key = RetentionKey("package_tree" if category == "package" else "state_tree", digest)
        result.setdefault(key, []).append(StoredTreeReference(table, identity, column, account_id))

    for revision in index.revisions:
        add(
            "skill_revisions",
            (str(revision.id),),
            None,
            "package",
            revision.tree_digest,
            "tree_digest",
        )
    for local in index.local_revisions:
        add(
            "account_local_skill_revisions",
            (str(local.id),),
            local.account_id,
            "state",
            local.tree_digest,
            "tree_digest",
        )
    for checkpoint in index.checkpoints:
        add(
            "skill_checkpoints",
            (str(checkpoint.id),),
            checkpoint.account_id,
            "state",
            checkpoint.tree_digest,
            "tree_digest",
        )
    for snapshot in index.snapshots:
        add(
            "session_skill_snapshots",
            (str(snapshot.id),),
            snapshot.account_id,
            "state",
            snapshot.retained_tree_digest,
            "retained_tree_digest",
        )
    for finalization in index.finalizations:
        add(
            "skill_finalizations",
            (str(finalization.id),),
            finalization.account_id,
            "state",
            finalization.retained_tree_digest,
            "retained_tree_digest",
        )
    for publication in index.publications:
        add(
            "skill_publications",
            (str(publication.id),),
            publication.account_id,
            "state",
            publication.retained_current_tree_digest,
            "retained_current_tree_digest",
        )
    for migration in index.migrations:
        for column, digest in (
            ("retained_base_digest", migration.retained_base_digest),
            ("retained_current_digest", migration.retained_current_digest),
            ("retained_incoming_digest", migration.retained_incoming_digest),
        ):
            add(
                "skill_branch_preparations",
                (str(migration.id),),
                migration.account_id,
                "state",
                digest,
                column,
            )
    for choice in index.choices:
        add(
            "skill_resolution_choices",
            (str(choice.publication_id), choice.selector_key),
            choice.account_id,
            "state",
            choice.retained_tree_digest,
            "retained_tree_digest",
        )
    for grant in index.migration_content:
        add(
            "skill_migration_resolution_content",
            (str(grant.migration_id), grant.tree_digest),
            grant.account_id,
            "state",
            grant.retained_tree_digest,
            "retained_tree_digest",
        )
    return {key: tuple(sorted(rows)) for key, rows in result.items()}
