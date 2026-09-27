"""
保存账户目录、安装版本分支及完整目录树中的不可变 checkpoint 视图。
"""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin
from agent_remote_server.models.skill_history import SkillHistoryMixin


class AccountSkillDirectoryState(TimestampMixin, Base):
    """
    账户目录唯一权威和纪元，空技能库不会使其自动降级。
    """

    __tablename__ = "account_skill_directory_states"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", name="skill_directory_owner_uq"),
        CheckConstraint(
            "mode IN ('legacy', 'migrating', 'managed_v1')", name="skill_directory_mode_ck"
        ),
        CheckConstraint("epoch >= 1", name="skill_directory_epoch_ck"),
        CheckConstraint("head_scope = 'directory'", name="skill_directory_scope_ck"),
        CheckConstraint(
            "mode <> 'managed_v1' OR head_checkpoint_id IS NOT NULL",
            name="skill_directory_managed_head_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "tool_type"],
            ["tool_accounts.user_id", "tool_accounts.id", "tool_accounts.tool_type"],
            name="skill_directory_account_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "head_scope", "head_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_directory_head_fk",
            use_alter=True,
        ),
    )
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="legacy")
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    head_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    head_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)


class AccountSkillState(IdMixin, TimestampMixin, Base):
    """
    一个账户在某个安装纪元与原始版本上的独立运行分支。
    """

    __tablename__ = "account_skill_states"
    __table_args__ = (
        UniqueConstraint("user_id", "account_id", "id", name="skill_state_owner_uq"),
        UniqueConstraint(
            "account_id",
            "installation_id",
            "installation_epoch",
            "base_revision_id",
            name="skill_state_branch_uq",
        ),
        UniqueConstraint(
            "user_id",
            "account_id",
            "installation_id",
            "installation_epoch",
            "id",
            name="skill_state_installation_identity_uq",
        ),
        CheckConstraint("epoch >= 1", name="skill_state_epoch_ck"),
        UniqueConstraint(
            "account_id", "local_skill_id", "local_revision_id", name="skill_state_local_branch_uq"
        ),
        CheckConstraint(
            "(installation_id IS NOT NULL AND base_revision_id IS NOT NULL "
            "AND local_skill_id IS NULL AND local_revision_id IS NULL) OR "
            "(installation_id IS NULL AND base_revision_id IS NULL AND local_skill_id IS NOT NULL "
            "AND local_revision_id IS NOT NULL AND installation_epoch = 1)",
            name="skill_state_origin_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "local_skill_id", "local_revision_id"],
            [
                "account_local_skill_revisions.user_id",
                "account_local_skill_revisions.account_id",
                "account_local_skill_revisions.local_skill_id",
                "account_local_skill_revisions.id",
            ],
            name="skill_state_local_revision_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_state_directory_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "installation_epoch"],
            [
                "skill_installation_epochs.user_id",
                "skill_installation_epochs.installation_id",
                "skill_installation_epochs.epoch",
            ],
            name="skill_state_installation_epoch_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "base_revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_state_revision_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "id", "head_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_state_head_fk",
            use_alter=True,
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    installation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    installation_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    base_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    local_skill_id: Mapped[UUID | None] = mapped_column(nullable=True)
    local_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    head_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    expired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SkillCheckpoint(SkillHistoryMixin, IdMixin, TimestampMixin, Base):
    """
    完整目录树或其中的分支视图，退役后保留审计摘要而释放内容引用。
    """

    __tablename__ = "skill_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_id",
            "scope",
            "id",
            "content_digest",
            name="skill_checkpoint_backing_identity_uq",
        ),
        CheckConstraint(
            "state_epoch IS NULL OR (scope = 'item' AND state_epoch >= 1)",
            name="skill_checkpoint_state_epoch_ck",
        ),
        CheckConstraint(
            "directory_epoch IS NULL OR (scope = 'directory' AND directory_epoch >= 1)",
            name="skill_checkpoint_directory_epoch_ck",
        ),
        CheckConstraint(
            "backing_scope = 'directory' AND (backing_directory_id IS NULL OR scope = 'item')",
            name="skill_checkpoint_backing_scope_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "backing_scope", "backing_directory_id", "content_digest"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
                "skill_checkpoints.content_digest",
            ],
            name="skill_checkpoint_backing_fk",
        ),
        UniqueConstraint("user_id", "account_id", "id", name="skill_checkpoint_owner_uq"),
        UniqueConstraint("user_id", "account_id", "scope", "id", name="skill_checkpoint_scope_uq"),
        UniqueConstraint(
            "user_id",
            "account_id",
            "scope",
            "id",
            "tree_digest",
            name="skill_checkpoint_content_uq",
        ),
        UniqueConstraint(
            "user_id", "account_id", "state_id", "id", name="skill_checkpoint_branch_uq"
        ),
        CheckConstraint("category = 'state'", name="skill_checkpoint_category_ck"),
        CheckConstraint(
            "(scope = 'directory' AND state_id IS NULL AND subtree_prefix = '') OR "
            "(scope = 'item' AND state_id IS NOT NULL AND subtree_prefix <> '')",
            name="skill_checkpoint_view_ck",
        ),
        CheckConstraint(
            "(retained = true AND tree_digest IS NOT NULL AND tree_digest = content_digest) OR "
            "(retained = false AND tree_digest IS NULL)",
            name="skill_checkpoint_retention_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_checkpoint_directory_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "state_id"],
            [
                "account_skill_states.user_id",
                "account_skill_states.account_id",
                "account_skill_states.id",
            ],
            name="skill_checkpoint_state_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_checkpoint_tree_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "parent_id"],
            ["skill_checkpoints.user_id", "skill_checkpoints.account_id", "skill_checkpoints.id"],
            name="skill_checkpoint_parent_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    state_id: Mapped[UUID | None] = mapped_column(nullable=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subtree_prefix: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    parent_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_session_reference_id: Mapped[UUID | None] = mapped_column(nullable=True)
    state_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    directory_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    backing_directory_id: Mapped[UUID | None] = mapped_column(nullable=True)
    backing_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    retained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalid_skill_format: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SkillDirectoryMember(Base):
    """
    目录 checkpoint 的不可变分支成员，不把未物化条目误判成删除。
    """

    __tablename__ = "skill_directory_members"
    __table_args__ = (
        UniqueConstraint(
            "directory_checkpoint_id", "state_id", name="skill_directory_member_state_uq"
        ),
        CheckConstraint("directory_scope = 'directory'", name="skill_member_scope_ck"),
        CheckConstraint(
            "entry_name <> '' AND entry_name NOT IN ('ego-browser', 'agent-remote-device')",
            name="skill_member_name_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "directory_scope", "directory_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_member_directory_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "state_id", "checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_member_checkpoint_fk",
        ),
    )
    directory_checkpoint_id: Mapped[UUID] = mapped_column(primary_key=True)
    entry_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    directory_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    state_id: Mapped[UUID] = mapped_column(nullable=False)
    checkpoint_id: Mapped[UUID] = mapped_column(nullable=False)
