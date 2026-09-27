"""
保存迁移专用内容授权、单调解决计划与不可变回执。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036_skill_migration_resolution"
down_revision = "0035_skill_incremental_migration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为迁移受理增加同用户账户的完整人工内容与计划引用。
    """
    op.create_unique_constraint(
        "skill_preparation_owner_uq", "skill_branch_preparations", ["user_id", "account_id", "id"]
    )
    op.create_table(
        "skill_migration_resolution_uploads",
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("migration_id", sa.Uuid(), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope = 'account_directory'", name="skill_migration_resolution_upload_scope_ck"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            ["skill_branch_preparations." + name for name in ("user_id", "account_id", "id")],
            name="skill_migration_resolution_upload_migration_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "upload_id", "tree_digest", "scope"],
            ["skill_content_uploads." + name for name in ("user_id", "id", "tree_digest", "scope")],
            name="skill_migration_resolution_upload_content_fk",
        ),
        sa.PrimaryKeyConstraint("upload_id"),
    )
    op.create_table(
        "skill_migration_resolution_content",
        sa.Column("migration_id", sa.Uuid(), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category = 'state'", name="skill_migration_resolution_content_category_ck"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            ["skill_branch_preparations." + name for name in ("user_id", "account_id", "id")],
            name="skill_migration_resolution_content_migration_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            ["skill_stored_trees." + name for name in ("user_id", "category", "digest")],
            name="skill_migration_resolution_content_tree_fk",
        ),
        sa.PrimaryKeyConstraint("migration_id", "tree_digest"),
        sa.UniqueConstraint(
            "user_id",
            "account_id",
            "migration_id",
            "category",
            "tree_digest",
            name="skill_migration_resolution_content_owner_uq",
        ),
    )
    op.create_table(
        "skill_migration_resolution_operations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("migration_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "plan_revision >= 0", name="skill_migration_resolution_operation_revision_ck"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            [
                "skill_branch_preparations.user_id",
                "skill_branch_preparations.account_id",
                "skill_branch_preparations.id",
            ],
            name="skill_migration_resolution_operation_migration_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="skill_migration_resolution_operation_key_uq"
        ),
    )
    op.create_table(
        "skill_migration_resolution_plans",
        sa.Column("migration_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="skill_migration_resolution_plan_revision_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            [
                "skill_branch_preparations.user_id",
                "skill_branch_preparations.account_id",
                "skill_branch_preparations.id",
            ],
            name="skill_migration_resolution_plan_migration_fk",
        ),
        sa.PrimaryKeyConstraint("migration_id"),
        sa.UniqueConstraint(
            "user_id", "account_id", "migration_id", name="skill_migration_resolution_plan_owner_uq"
        ),
    )
    op.create_table(
        "skill_migration_resolution_choices",
        sa.Column("migration_id", sa.Uuid(), nullable=False),
        sa.Column("selector_key", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=4096), nullable=True),
        sa.Column("unit_json", postgresql.JSONB(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(kind IN ('current', 'incoming') AND tree_digest IS NULL) OR "
            "(kind IN ('file', 'directory') AND tree_digest IS NOT NULL)",
            name="skill_migration_resolution_choice_tree_ck",
        ),
        sa.CheckConstraint(
            "category = 'state'", name="skill_migration_resolution_choice_category_ck"
        ),
        sa.CheckConstraint(
            "kind <> 'directory' OR path IS NULL",
            name="skill_migration_resolution_choice_directory_ck",
        ),
        sa.CheckConstraint(
            "kind <> 'file' OR path IS NOT NULL", name="skill_migration_resolution_choice_file_ck"
        ),
        sa.CheckConstraint(
            "kind IN ('current', 'incoming', 'file', 'directory')",
            name="skill_migration_resolution_choice_kind_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            [
                "skill_migration_resolution_plans.user_id",
                "skill_migration_resolution_plans.account_id",
                "skill_migration_resolution_plans.migration_id",
            ],
            name="skill_migration_resolution_choice_plan_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id", "category", "tree_digest"],
            [
                "skill_migration_resolution_content." + column
                for column in ("user_id", "account_id", "migration_id", "category", "tree_digest")
            ],
            name="skill_migration_resolution_choice_content_fk",
        ),
        sa.PrimaryKeyConstraint("migration_id", "selector_key"),
    )


def downgrade() -> None:
    """
    已存在计划或幂等历史时拒绝丢弃人工处理内容的恢复引用。
    """
    for table in (
        "skill_migration_resolution_uploads",
        "skill_migration_resolution_content",
        "skill_migration_resolution_plans",
        "skill_migration_resolution_choices",
        "skill_migration_resolution_operations",
    ):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).scalar():
            raise RuntimeError("migration resolution history must be retained before downgrade")
    op.drop_table("skill_migration_resolution_choices")
    op.drop_table("skill_migration_resolution_plans")
    op.drop_table("skill_migration_resolution_operations")
    op.drop_table("skill_migration_resolution_content")
    op.drop_table("skill_migration_resolution_uploads")
    op.drop_constraint("skill_preparation_owner_uq", "skill_branch_preparations", type_="unique")
