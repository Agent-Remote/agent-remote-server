"""
增加账户本地候选身份、原始目录视图和互斥运行分支来源。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_account_local_skills"
down_revision = "0029_skill_finalization_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    先建立本地来源及版本，再允许运行分支引用这类独立身份。
    """
    op.create_table(
        "account_local_skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source_scope", sa.String(16), nullable=False),
        sa.Column("source_checkpoint_id", sa.Uuid(), nullable=False),
        sa.Column("default_revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "account_id", "id", name="skill_local_owner_uq"),
        sa.UniqueConstraint(
            "account_id", "source_checkpoint_id", "name", name="skill_local_source_uq"
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'active', 'removed')", name="skill_local_status_ck"
        ),
        sa.CheckConstraint("source_scope = 'directory'", name="skill_local_source_scope_ck"),
        sa.CheckConstraint(
            "name <> '' AND name NOT IN ('ego-browser', 'agent-remote-device')",
            name="skill_local_name_ck",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR default_revision_id IS NOT NULL",
            name="skill_local_active_revision_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_local_directory_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "source_scope", "source_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_local_source_fk",
        ),
    )
    op.create_index(
        "skill_local_active_name_uq",
        "account_local_skills",
        ["account_id", "name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "account_local_skill_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("local_skill_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("tree_digest", sa.String(64), nullable=True),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("subtree_prefix", sa.String(64), nullable=False),
        sa.Column("retained", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "account_id", "local_skill_id", "id", name="skill_local_revision_owner_uq"
        ),
        sa.UniqueConstraint("local_skill_id", "number", name="skill_local_revision_number_uq"),
        sa.CheckConstraint("number >= 1", name="skill_local_revision_number_ck"),
        sa.CheckConstraint(
            "category = 'state' AND subtree_prefix <> ''", name="skill_local_revision_scope_ck"
        ),
        sa.CheckConstraint(
            "(retained = true AND tree_digest IS NOT NULL AND tree_digest = content_digest) OR "
            "(retained = false AND tree_digest IS NULL)",
            name="skill_local_revision_retention_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "local_skill_id"],
            [
                "account_local_skills.user_id",
                "account_local_skills.account_id",
                "account_local_skills.id",
            ],
            name="skill_local_revision_skill_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_local_revision_tree_fk",
        ),
    )
    op.create_foreign_key(
        "skill_local_default_revision_fk",
        "account_local_skills",
        "account_local_skill_revisions",
        ["user_id", "account_id", "id", "default_revision_id"],
        ["user_id", "account_id", "local_skill_id", "id"],
    )
    op.alter_column(
        "account_skill_states", "installation_id", existing_type=sa.Uuid(), nullable=True
    )
    op.alter_column(
        "account_skill_states", "base_revision_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column("account_skill_states", sa.Column("local_skill_id", sa.Uuid(), nullable=True))
    op.add_column("account_skill_states", sa.Column("local_revision_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "skill_state_local_branch_uq",
        "account_skill_states",
        ["account_id", "local_skill_id", "local_revision_id"],
    )
    op.create_check_constraint(
        "skill_state_origin_ck",
        "account_skill_states",
        "(installation_id IS NOT NULL AND base_revision_id IS NOT NULL "
        "AND local_skill_id IS NULL AND local_revision_id IS NULL) OR "
        "(installation_id IS NULL AND base_revision_id IS NULL AND local_skill_id IS NOT NULL "
        "AND local_revision_id IS NOT NULL AND installation_epoch = 1)",
    )
    op.create_foreign_key(
        "skill_state_local_revision_fk",
        "account_skill_states",
        "account_local_skill_revisions",
        ["user_id", "account_id", "local_skill_id", "local_revision_id"],
        ["user_id", "account_id", "local_skill_id", "id"],
    )


def downgrade() -> None:
    """
    有本地身份时在任何写入前拒绝降级，空 schema 则可完整恢复原约束。
    """
    if op.get_bind().execute(sa.text("SELECT 1 FROM account_local_skills LIMIT 1")).scalar():
        raise RuntimeError("account-local skills cannot be downgraded without losing ownership")
    op.drop_constraint("skill_state_local_revision_fk", "account_skill_states", type_="foreignkey")
    op.drop_constraint("skill_state_origin_ck", "account_skill_states", type_="check")
    op.drop_constraint("skill_state_local_branch_uq", "account_skill_states", type_="unique")
    op.drop_column("account_skill_states", "local_revision_id")
    op.drop_column("account_skill_states", "local_skill_id")
    op.alter_column(
        "account_skill_states", "installation_id", existing_type=sa.Uuid(), nullable=False
    )
    op.alter_column(
        "account_skill_states", "base_revision_id", existing_type=sa.Uuid(), nullable=False
    )
    op.drop_constraint(
        "skill_local_default_revision_fk", "account_local_skills", type_="foreignkey"
    )
    op.drop_table("account_local_skill_revisions")
    op.drop_index("skill_local_active_name_uq", table_name="account_local_skills")
    op.drop_table("account_local_skills")
