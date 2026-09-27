"""
保存冲突解决计划、人工内容引用及不可变幂等回执。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_skill_resolution_plans"
down_revision = "0031_skill_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为完整发布尝试增加归属一致的解决计划和引用。
    """
    op.create_table(
        "skill_resolution_operations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("plan_revision >= 0", name="skill_resolution_operation_revision_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_publications.user_id",
                "skill_publications.account_id",
                "skill_publications.id",
            ],
            name="skill_resolution_operation_publication_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_resolution_operation_key_uq"),
    )
    op.create_table(
        "skill_resolution_plans",
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="skill_resolution_plan_revision_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_publications.user_id",
                "skill_publications.account_id",
                "skill_publications.id",
            ],
            name="skill_resolution_plan_publication_fk",
        ),
        sa.PrimaryKeyConstraint("publication_id"),
        sa.UniqueConstraint(
            "user_id", "account_id", "publication_id", name="skill_resolution_plan_owner_uq"
        ),
    )
    op.create_table(
        "skill_resolution_choices",
        sa.Column("publication_id", sa.Uuid(), nullable=False),
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
            name="skill_resolution_choice_tree_ck",
        ),
        sa.CheckConstraint("category = 'state'", name="skill_resolution_choice_category_ck"),
        sa.CheckConstraint(
            "kind <> 'directory' OR path IS NULL", name="skill_resolution_choice_directory_ck"
        ),
        sa.CheckConstraint(
            "kind <> 'file' OR path IS NOT NULL", name="skill_resolution_choice_file_ck"
        ),
        sa.CheckConstraint(
            "kind IN ('current', 'incoming', 'file', 'directory')",
            name="skill_resolution_choice_kind_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_resolution_plans.user_id",
                "skill_resolution_plans.account_id",
                "skill_resolution_plans.publication_id",
            ],
            name="skill_resolution_choice_plan_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_resolution_choice_content_fk",
        ),
        sa.PrimaryKeyConstraint("publication_id", "selector_key"),
    )


def downgrade() -> None:
    """
    已存在计划或幂等历史时拒绝丢弃人工处理内容的恢复引用。
    """
    for table in ("skill_resolution_plans", "skill_resolution_operations"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).scalar():
            raise RuntimeError("resolution history must be retained before downgrade")
    op.drop_table("skill_resolution_choices")
    op.drop_table("skill_resolution_plans")
    op.drop_table("skill_resolution_operations")
