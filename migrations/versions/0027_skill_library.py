"""
增加用户技能库、版本激活历史、逐字段覆盖及幂等操作。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_skill_library"
down_revision: str | None = "0026_skill_content_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建私有用户库并建立同用户、同技能和同账户工具的外键边界。
    """
    op.create_unique_constraint(
        "tool_accounts_owner_tool_uq", "tool_accounts", ["user_id", "id", "tool_type"]
    )
    op.create_table(
        "skill_libraries",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 0", name="skill_library_generation_ck"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "skill_installations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column(
            "source_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "tracking_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("default_revision_id", sa.Uuid(), nullable=True),
        sa.Column("default_enabled", sa.Boolean(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("epoch >= 1", name="skill_installation_epoch_ck"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["skill_libraries.user_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id", name="skill_installation_owner_uq"),
        sa.UniqueConstraint("user_id", "source_key", name="skill_installation_source_uq"),
    )
    op.create_index(
        "skill_installation_active_name_uq",
        "skill_installations",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("removed = false"),
        sqlite_where=sa.text("removed = 0"),
    )
    op.create_table(
        "skill_installation_epochs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("epoch >= 1", name="skill_epoch_number_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_epoch_installation_fk",
        ),
        sa.PrimaryKeyConstraint("user_id", "installation_id", "epoch"),
    )
    op.create_table(
        "skill_revisions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "provenance_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("retained", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("category = 'package'", name="skill_revision_category_ck"),
        sa.CheckConstraint("number >= 1", name="skill_revision_number_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_revision_tree_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_revision_installation_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_id", "content_digest", name="skill_revision_content_uq"),
        sa.UniqueConstraint("installation_id", "number", name="skill_revision_number_uq"),
        sa.UniqueConstraint("user_id", "installation_id", "id", name="skill_revision_owner_uq"),
    )
    op.create_table(
        "skill_tool_overrides",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("tool_type", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_tool_revision_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_tool_installation_fk",
        ),
        sa.PrimaryKeyConstraint("user_id", "installation_id", "tool_type"),
    )
    op.create_table(
        "skill_account_overrides",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("tool_type", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "tool_type"],
            ["tool_accounts.user_id", "tool_accounts.id", "tool_accounts.tool_type"],
            name="skill_override_account_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_account_revision_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_account_installation_fk",
        ),
        sa.PrimaryKeyConstraint("user_id", "installation_id", "account_id"),
    )
    op.create_table(
        "skill_activations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_activation_revision_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("installation_id", "generation", name="skill_activation_generation_uq"),
    )
    op.create_table(
        "skill_source_observations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "provenance_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_observation_revision_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "skill_operations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "request_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("committed", sa.Boolean(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("replacement_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "id", name="skill_operation_owner_uq"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_operation_key_uq"),
    )

    op.create_foreign_key(
        "skill_installation_default_revision_fk",
        "skill_installations",
        "skill_revisions",
        ["user_id", "id", "default_revision_id"],
        ["user_id", "installation_id", "id"],
    )


def downgrade() -> None:
    """
    删除技能库元数据，内容卷和上传记录由独立存储迁移管理。
    """
    op.drop_constraint(
        "skill_installation_default_revision_fk", "skill_installations", type_="foreignkey"
    )
    op.drop_table("skill_operations")
    op.drop_table("skill_source_observations")
    op.drop_table("skill_activations")
    op.drop_table("skill_account_overrides")
    op.drop_table("skill_tool_overrides")
    op.drop_table("skill_revisions")
    op.drop_table("skill_installation_epochs")
    op.drop_table("skill_installations")
    op.drop_table("skill_libraries")
    op.drop_constraint("tool_accounts_owner_tool_uq", "tool_accounts", type_="unique")
