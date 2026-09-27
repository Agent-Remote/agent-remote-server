"""
登记随实际内容生命周期消失的续扫归属，不从历史摘要回填新的删除资格。
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_skill_prune_claims"
down_revision = "0043_skill_content_deletions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    新增空归属表和明确的生命周期外键；原历史、树、对象及额度保持原样。
    """
    op.create_table(
        "skill_prune_content_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.UniqueConstraint("id", name="skill_prune_claim_id_uq"),
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("source_key", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(8), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("tree_digest", sa.String(64), nullable=True),
        sa.Column("object_digest", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("category = 'state'", name="skill_prune_claim_category_ck"),
        sa.CheckConstraint(
            "(kind = 'tree' AND tree_digest IS NOT NULL "
            "AND tree_digest = digest AND object_digest IS NULL) OR "
            "(kind = 'object' AND object_digest IS NOT NULL "
            "AND object_digest = digest AND tree_digest IS NULL)",
            name="skill_prune_claim_kind_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_prune_claim_account_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            ondelete="CASCADE",
            name="skill_prune_claim_tree_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "object_digest"],
            [
                "skill_content_objects.user_id",
                "skill_content_objects.category",
                "skill_content_objects.digest",
            ],
            ondelete="CASCADE",
            name="skill_prune_claim_object_fk",
        ),
    )
    op.create_index(
        "skill_prune_claim_tree_idx",
        "skill_prune_content_claims",
        ["user_id", "category", "tree_digest"],
    )
    op.create_index(
        "skill_prune_claim_object_idx",
        "skill_prune_content_claims",
        ["user_id", "category", "object_digest"],
    )


def downgrade() -> None:
    """
    有任何未完成的内容续扫归属时拒绝降级，先预检再改变 schema。
    """
    claims = sa.table("skill_prune_content_claims", sa.column("digest"))
    if op.get_bind().execute(sa.select(claims.c.digest).limit(1)).first():
        raise RuntimeError("skill prune content claims must be preserved before downgrade")
    op.drop_table("skill_prune_content_claims")
