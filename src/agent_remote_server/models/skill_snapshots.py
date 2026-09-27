"""
保存精确会话内容引用、物化成员和节点绑定的收尾提交。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin
from agent_remote_server.models.skill_history import SkillHistoryMixin
from agent_remote_server.models.skill_retired_content import (
    SkillRetiredContentMixin,
    retained_digest,
)

_JSON = JSON().with_variant(JSONB(), "postgresql")


class SessionSkillSnapshot(
    SkillRetiredContentMixin, SkillHistoryMixin, IdMixin, TimestampMixin, Base
):
    """
    单次事务固定会话归属、规则代数、目录纪元和实际准备树。
    """

    __tablename__ = "session_skill_snapshots"
    __table_args__ = (
        CheckConstraint(
            "content_retired_at IS NULL OR (status IN ('retained', 'cancelled'))",
            name="skill_snapshot_retired_ck",
        ),
        UniqueConstraint("session_reference_id", name="skill_snapshot_session_uq"),
        UniqueConstraint("user_id", "account_id", "id", name="skill_snapshot_owner_uq"),
        UniqueConstraint("user_id", "account_id", "node_id", "id", name="skill_snapshot_node_uq"),
        CheckConstraint(
            "library_generation >= 0 AND directory_epoch >= 1", name="skill_snapshot_generation_ck"
        ),
        CheckConstraint(
            "category = 'state' AND starting_scope = 'directory'", name="skill_snapshot_scope_ck"
        ),
        CheckConstraint(
            "session_id IS NULL OR session_id = session_reference_id",
            name="skill_snapshot_session_ck",
        ),
        CheckConstraint(
            "session_id IS NOT NULL OR status IN ('retained', 'cancelled')",
            name="skill_snapshot_live_session_ck",
        ),
        CheckConstraint(
            "runtime_backend IN ('native', 'docker_sandbox')", name="skill_snapshot_backend_ck"
        ),
        CheckConstraint(
            "status IN ('reserved', 'started', 'finalizing', 'retained', 'cancelled')",
            name="skill_snapshot_status_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_snapshot_directory_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "node_id", "session_id"],
            ["sessions.user_id", "sessions.tool_account_id", "sessions.node_id", "sessions.id"],
            name="skill_snapshot_session_fk",
        ),
        ForeignKeyConstraint(
            ["node_id", "prepare_task_id"],
            ["node_tasks.node_id", "node_tasks.id"],
            name="skill_snapshot_task_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "starting_scope", "starting_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_snapshot_starting_head_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "retained_tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_snapshot_tree_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    session_id: Mapped[UUID | None] = mapped_column(nullable=True)
    session_reference_id: Mapped[UUID] = mapped_column(nullable=False)
    prepare_task_id: Mapped[UUID | None] = mapped_column(nullable=True)
    runtime_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    library_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    directory_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    starting_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    starting_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    tree_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    system_releases_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)

    retained_tree_digest: Mapped[str | None] = retained_digest("tree_digest")


class SessionSkillSnapshotItem(Base):
    """
    精确快照中实际暴露的分支、起始 checkpoint 和字段解析原因。
    """

    __tablename__ = "session_skill_snapshot_items"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "entry_name", name="skill_snapshot_item_name_uq"),
        CheckConstraint("state_epoch >= 1", name="skill_snapshot_item_epoch_ck"),
        CheckConstraint(
            "entry_name <> '' AND entry_name NOT IN ('ego-browser', 'agent-remote-device')",
            name="skill_snapshot_item_name_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "snapshot_id"],
            [
                "session_skill_snapshots.user_id",
                "session_skill_snapshots.account_id",
                "session_skill_snapshots.id",
            ],
            name="skill_snapshot_item_snapshot_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "state_id", "checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_snapshot_item_checkpoint_fk",
        ),
    )
    snapshot_id: Mapped[UUID] = mapped_column(primary_key=True)
    state_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    entry_name: Mapped[str] = mapped_column(String(64), nullable=False)
    state_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_id: Mapped[UUID] = mapped_column(nullable=False)
    resolution_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)


class SkillFinalization(SkillRetiredContentMixin, SkillHistoryMixin, IdMixin, TimestampMixin, Base):
    """
    每个快照只提交一份完整输入，内容持久化与发布状态严格分离。
    """

    __tablename__ = "skill_finalizations"
    __table_args__ = (
        CheckConstraint(
            "persisted_at IS NULL OR status <> 'upload_pending'",
            name="skill_finalization_persisted_time_ck",
        ),
        CheckConstraint(
            "content_retired_at IS NULL OR (status IN ('published', 'conflicted', 'detached'))",
            name="skill_finalization_retired_ck",
        ),
        UniqueConstraint("snapshot_id", name="skill_finalization_snapshot_uq"),
        UniqueConstraint("user_id", "account_id", "id", name="skill_finalization_owner_uq"),
        UniqueConstraint("user_id", "id", "incoming_digest", name="skill_finalization_input_uq"),
        UniqueConstraint("user_id", "idempotency_key", name="skill_finalization_key_uq"),
        CheckConstraint(
            "category = 'state' AND checkpoint_scope = 'directory'",
            name="skill_finalization_scope_ck",
        ),
        CheckConstraint(
            "status IN ('upload_pending', 'persisted', 'persisted_unclean', "
            "'published', 'conflicted', 'detached')",
            name="skill_finalization_status_ck",
        ),
        CheckConstraint(
            "(unclean = false AND status <> 'persisted_unclean') OR "
            "(unclean = true AND status IN ('upload_pending', 'persisted_unclean', 'detached'))",
            name="skill_finalization_unclean_ck",
        ),
        CheckConstraint(
            "(status = 'upload_pending' AND tree_digest IS NULL AND checkpoint_id IS NULL) OR "
            "(status <> 'upload_pending' AND tree_digest IS NOT NULL "
            "AND checkpoint_id IS NOT NULL AND tree_digest = incoming_digest)",
            name="skill_finalization_retained_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "node_id", "snapshot_id"],
            [
                "session_skill_snapshots.user_id",
                "session_skill_snapshots.account_id",
                "session_skill_snapshots.node_id",
                "session_skill_snapshots.id",
            ],
            name="skill_finalization_snapshot_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "retained_tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_finalization_tree_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "checkpoint_id", "tree_digest"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
                "skill_checkpoints.content_digest",
            ],
            name="skill_finalization_checkpoint_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    node_id: Mapped[UUID] = mapped_column(nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    incoming_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    unclean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="upload_pending")
    persisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retained_tree_digest: Mapped[str | None] = retained_digest("tree_digest")
