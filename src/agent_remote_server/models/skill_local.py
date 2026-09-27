"""
保存账户独有技能身份与完整状态树中的不可变初始版本。
"""

from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin
from agent_remote_server.models.skill_history import SkillHistoryMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class AccountLocalSkill(IdMixin, TimestampMixin, Base):
    """
    待发布候选不进入用户库或其他账户，同名并发候选也不合并身份。
    """

    __tablename__ = "account_local_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", "id", name="skill_local_owner_uq"),
        UniqueConstraint(
            "account_id", "source_checkpoint_id", "name", name="skill_local_source_uq"
        ),
        Index(
            "skill_local_active_name_uq",
            "account_id",
            "name",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        CheckConstraint("status IN ('staged', 'active', 'removed')", name="skill_local_status_ck"),
        CheckConstraint("source_scope = 'directory'", name="skill_local_source_scope_ck"),
        CheckConstraint(
            "name <> '' AND name NOT IN ('ego-browser', 'agent-remote-device')",
            name="skill_local_name_ck",
        ),
        CheckConstraint(
            "status <> 'active' OR default_revision_id IS NOT NULL",
            name="skill_local_active_revision_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_local_directory_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "source_scope", "source_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_local_source_fk",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "id", "default_revision_id"],
            [
                "account_local_skill_revisions.user_id",
                "account_local_skill_revisions.account_id",
                "account_local_skill_revisions.local_skill_id",
                "account_local_skill_revisions.id",
            ],
            name="skill_local_default_revision_fk",
            use_alter=True,
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staged")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    source_checkpoint_id: Mapped[UUID] = mapped_column(nullable=False)
    default_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class AccountLocalSkillRevision(SkillHistoryMixin, IdMixin, TimestampMixin, Base):
    """
    初始包保留完整目录及子树前缀，不能被其他账户用 pin 引用。
    """

    __tablename__ = "account_local_skill_revisions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "account_id", "local_skill_id", "id", name="skill_local_revision_owner_uq"
        ),
        UniqueConstraint("local_skill_id", "number", name="skill_local_revision_number_uq"),
        CheckConstraint("number >= 1", name="skill_local_revision_number_ck"),
        CheckConstraint(
            "category = 'state' AND subtree_prefix <> ''", name="skill_local_revision_scope_ck"
        ),
        CheckConstraint(
            "(retained = true AND tree_digest IS NOT NULL AND tree_digest = content_digest) OR "
            "(retained = false AND tree_digest IS NULL)",
            name="skill_local_revision_retention_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "local_skill_id"],
            [
                "account_local_skills.user_id",
                "account_local_skills.account_id",
                "account_local_skills.id",
            ],
            name="skill_local_revision_skill_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_local_revision_tree_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    local_skill_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    subtree_prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    retained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
