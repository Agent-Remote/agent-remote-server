"""
记录真实有效分支与独立版本准备的完整输入和受理结果。
"""

from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
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


class SkillEffectiveBranch(TimestampMixin, Base):
    """
    最后成功预约的真实成员，晚到收尾不能改变自动迁移来源。
    """

    __tablename__ = "skill_effective_branches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "account_id", "installation_id", "installation_epoch", "state_id"],
            [
                "account_skill_states." + name
                for name in ("user_id", "account_id", "installation_id", "installation_epoch", "id")
            ],
            name="skill_effective_branch_fk",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "state_id"],
            ["session_skill_snapshot_items.snapshot_id", "session_skill_snapshot_items.state_id"],
            name="skill_effective_member_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(primary_key=True)
    installation_epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state_id: Mapped[UUID] = mapped_column(nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(nullable=False)


class SkillBranchPreparation(
    SkillRetiredContentMixin, SkillHistoryMixin, IdMixin, TimestampMixin, Base
):
    """
    准备回执保留精确比较输入，冲突不会借用会话收尾的身份和含义。
    """

    __tablename__ = "skill_branch_preparations"
    __table_args__ = (
        CheckConstraint(
            "content_retired_at IS NULL OR (status <> 'conflicted')",
            name="skill_preparation_retired_ck",
        ),
        UniqueConstraint("user_id", "account_id", "id", name="skill_preparation_owner_uq"),
        UniqueConstraint("user_id", "idempotency_key", name="skill_preparation_key_uq"),
        UniqueConstraint("recomputed_from_id", name="skill_preparation_recomputed_uq"),
        CheckConstraint(
            "recomputed_from_id IS NULL OR recomputed_from_id <> id",
            name="skill_preparation_predecessor_ck",
        ),
        CheckConstraint(
            "replacement_id IS NULL OR (replacement_id <> id AND status = 'superseded')",
            name="skill_preparation_replacement_ck",
        ),
        CheckConstraint(
            "superseded_reason IS NULL OR status = 'superseded'",
            name="skill_preparation_reason_ck",
        ),
        *(
            ForeignKeyConstraint(
                ["user_id", "account_id", column],
                ["skill_branch_preparations." + name for name in ("user_id", "account_id", "id")],
                name="skill_preparation_" + label + "_fk",
            )
            for column, label in (
                ("recomputed_from_id", "predecessor"),
                ("replacement_id", "replacement"),
            )
        ),
        CheckConstraint(
            "status IN ('ready', 'conflicted', 'superseded')", name="skill_preparation_status_ck"
        ),
        CheckConstraint(
            "mode IN ('initial', 'forward', 'older', 'resume', 'incremental')",
            name="skill_preparation_mode_ck",
        ),
        UniqueConstraint(
            "source_state_id",
            "target_state_id",
            "source_epoch",
            "target_epoch",
            "directory_epoch",
            "migration_sequence",
            name="skill_migration_sequence_uq",
        ),
        CheckConstraint(
            "(mode IN ('forward', 'incremental') AND status = 'ready' "
            "AND migration_sequence IS NOT NULL AND migration_sequence >= 1) OR "
            "((mode NOT IN ('forward', 'incremental') OR status <> 'ready') "
            "AND migration_sequence IS NULL)",
            name="skill_migration_sequence_ck",
        ),
        CheckConstraint(
            "mode NOT IN ('forward', 'incremental') OR "
            "(source_state_id IS NOT NULL AND source_state_id <> target_state_id)",
            name="skill_migration_distinct_source_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "source_state_id", "base_checkpoint_id"],
            ["skill_checkpoints." + name for name in ("user_id", "account_id", "state_id", "id")],
            name="skill_migration_base_checkpoint_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "target_state_id", "current_checkpoint_id"],
            ["skill_checkpoints." + name for name in ("user_id", "account_id", "state_id", "id")],
            name="skill_migration_current_checkpoint_fk",
        ),
        CheckConstraint(
            "category = 'state' AND directory_scope = 'directory'",
            name="skill_preparation_scope_ck",
        ),
        CheckConstraint(
            "target_epoch >= 1 AND directory_epoch >= 1 AND library_generation >= 0",
            name="skill_preparation_epoch_ck",
        ),
        CheckConstraint(
            "(source_state_id IS NULL AND source_checkpoint_id IS NULL AND "
            "source_epoch IS NULL) OR "
            "(source_state_id IS NOT NULL AND source_checkpoint_id IS NOT NULL AND "
            "source_epoch IS NOT NULL AND source_epoch >= 1)",
            name="skill_preparation_source_ck",
        ),
        CheckConstraint(
            "(status = 'ready' AND result_checkpoint_id IS NOT NULL AND "
            "result_directory_id IS NOT NULL) OR "
            "(status <> 'ready' AND result_checkpoint_id IS NULL AND result_directory_id IS NULL)",
            name="skill_preparation_result_ck",
        ),
        *(
            ForeignKeyConstraint(
                [
                    "user_id",
                    "account_id",
                    "installation_id",
                    "installation_epoch",
                    side + "_state_id",
                ],
                [
                    "account_skill_states." + name
                    for name in (
                        "user_id",
                        "account_id",
                        "installation_id",
                        "installation_epoch",
                        "id",
                    )
                ],
                name="skill_preparation_" + side + "_state_fk",
            )
            for side in ("source", "target")
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "source_state_id", "source_checkpoint_id"],
            ["skill_checkpoints." + name for name in ("user_id", "account_id", "state_id", "id")],
            name="skill_preparation_source_checkpoint_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "target_state_id", "result_checkpoint_id"],
            ["skill_checkpoints." + name for name in ("user_id", "account_id", "state_id", "id")],
            name="skill_preparation_result_checkpoint_fk",
        ),
        *(
            ForeignKeyConstraint(
                ["user_id", "account_id", "directory_scope", name],
                [
                    "skill_checkpoints." + column
                    for column in ("user_id", "account_id", "scope", "id")
                ],
                name="skill_preparation_" + label + "_directory_fk",
            )
            for name, label in (
                ("directory_checkpoint_id", "input"),
                ("result_directory_id", "result"),
            )
        ),
        *(
            ForeignKeyConstraint(
                ["user_id", "category", "retained_" + side + "_digest"],
                [
                    "skill_stored_trees.user_id",
                    "skill_stored_trees.category",
                    "skill_stored_trees.digest",
                ],
                name="skill_preparation_" + side + "_tree_fk",
            )
            for side in ("base", "current", "incoming")
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    installation_id: Mapped[UUID] = mapped_column(nullable=False)
    installation_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    recomputed_from_id: Mapped[UUID | None] = mapped_column(nullable=True)
    replacement_id: Mapped[UUID | None] = mapped_column(nullable=True)
    superseded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_state_id: Mapped[UUID] = mapped_column(nullable=False)
    target_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_state_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    current_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    migration_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    directory_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    library_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    directory_scope: Mapped[str] = mapped_column(String(16), default="directory", nullable=False)
    directory_checkpoint_id: Mapped[UUID] = mapped_column(nullable=False)
    result_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result_directory_id: Mapped[UUID | None] = mapped_column(nullable=True)
    category: Mapped[str] = mapped_column(String(16), default="state", nullable=False)
    base_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    current_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    incoming_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    retained_base_digest: Mapped[str | None] = retained_digest("base_digest")
    retained_current_digest: Mapped[str | None] = retained_digest("current_digest")
    retained_incoming_digest: Mapped[str | None] = retained_digest("incoming_digest")
