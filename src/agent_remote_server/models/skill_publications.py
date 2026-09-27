"""
保存完整收尾发布尝试、冲突输入及精确分支前置条件。
"""

from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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

_JSON = JSON().with_variant(JSONB(), "postgresql")


class SkillPublication(SkillRetiredContentMixin, SkillHistoryMixin, IdMixin, TimestampMixin, Base):
    """
    每次尝试保留比较输入和完整结果，不用部分发布掩盖目录冲突。
    """

    __tablename__ = "skill_publications"
    __table_args__ = (
        CheckConstraint(
            "content_retired_at IS NULL OR (status <> 'conflicted')",
            name="skill_publication_retired_ck",
        ),
        UniqueConstraint("user_id", "account_id", "id", name="skill_publication_owner_uq"),
        UniqueConstraint("finalization_id", "attempt", name="skill_publication_attempt_uq"),
        CheckConstraint("attempt >= 1 AND directory_epoch >= 1", name="skill_publication_epoch_ck"),
        CheckConstraint(
            "status IN ('published', 'conflicted', 'detached', 'superseded')",
            name="skill_publication_status_ck",
        ),
        CheckConstraint(
            "category = 'state' AND checkpoint_scope = 'directory'",
            name="skill_publication_scope_ck",
        ),
        CheckConstraint(
            "(status = 'published' AND result_checkpoint_id IS NOT NULL) OR "
            "(status <> 'published' AND result_checkpoint_id IS NULL)",
            name="skill_publication_result_ck",
        ),
        CheckConstraint(
            "status = 'detached' OR "
            "(expected_directory_id IS NOT NULL AND current_tree_digest IS NOT NULL)",
            name="skill_publication_current_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "finalization_id"],
            [
                "skill_finalizations.user_id",
                "skill_finalizations.account_id",
                "skill_finalizations.id",
            ],
            name="skill_publication_finalization_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "expected_directory_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_publication_expected_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "result_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_publication_result_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "retained_current_tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_publication_tree_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    finalization_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    directory_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    expected_directory_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    current_tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conflicts_json: Mapped[list[dict[str, object]]] = mapped_column(
        _JSON, nullable=False, default=list
    )

    retained_current_tree_digest: Mapped[str | None] = retained_digest("current_tree_digest")


class SkillPublicationBranch(Base):
    """
    保存实际暴露分支的比较目标，未改动条目不被误判为需要发布的写入。
    """

    __tablename__ = "skill_publication_branches"
    __table_args__ = (
        CheckConstraint("state_epoch >= 1", name="skill_publication_branch_epoch_ck"),
        ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_publications.user_id",
                "skill_publications.account_id",
                "skill_publications.id",
            ],
            name="skill_publication_branch_attempt_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "state_id"],
            [
                "account_skill_states.user_id",
                "account_skill_states.account_id",
                "account_skill_states.id",
            ],
            name="skill_publication_branch_state_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "state_id", "expected_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_publication_branch_head_fk",
        ),
    )
    publication_id: Mapped[UUID] = mapped_column(primary_key=True)
    state_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    entry_name: Mapped[str] = mapped_column(String(64), nullable=False)
    state_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
