"""
保存账户目录原子发布结果、冲突比较树及分支前置条件。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_skill_publications"
down_revision = "0030_account_local_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为收尾增加归属唯一键并建立独立发布尝试和分支条件。
    """
    op.create_unique_constraint(
        "skill_finalization_owner_uq", "skill_finalizations", ["user_id", "account_id", "id"]
    )
    op.create_table(
        "skill_publications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("finalization_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("directory_epoch", sa.BigInteger(), nullable=False),
        sa.Column("checkpoint_scope", sa.String(length=16), nullable=False),
        sa.Column("expected_directory_id", sa.Uuid(), nullable=True),
        sa.Column("result_checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("current_tree_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "conflicts_json",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'published' AND result_checkpoint_id IS NOT NULL) OR "
            "(status <> 'published' AND result_checkpoint_id IS NULL)",
            name="skill_publication_result_ck",
        ),
        sa.CheckConstraint(
            "category = 'state' AND checkpoint_scope = 'directory'",
            name="skill_publication_scope_ck",
        ),
        sa.CheckConstraint(
            "status = 'detached' OR "
            "(expected_directory_id IS NOT NULL AND current_tree_digest IS NOT NULL)",
            name="skill_publication_current_ck",
        ),
        sa.CheckConstraint(
            "status IN ('published', 'conflicted', 'detached', 'superseded')",
            name="skill_publication_status_ck",
        ),
        sa.CheckConstraint(
            "attempt >= 1 AND directory_epoch >= 1", name="skill_publication_epoch_ck"
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "expected_directory_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_publication_expected_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "result_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_publication_result_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "finalization_id"],
            [
                "skill_finalizations.user_id",
                "skill_finalizations.account_id",
                "skill_finalizations.id",
            ],
            name="skill_publication_finalization_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "current_tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_publication_tree_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("finalization_id", "attempt", name="skill_publication_attempt_uq"),
        sa.UniqueConstraint("user_id", "account_id", "id", name="skill_publication_owner_uq"),
    )
    op.create_table(
        "skill_publication_branches",
        sa.Column("publication_id", sa.Uuid(), nullable=False),
        sa.Column("state_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("entry_name", sa.String(length=64), nullable=False),
        sa.Column("state_epoch", sa.BigInteger(), nullable=False),
        sa.Column("expected_checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.CheckConstraint("state_epoch >= 1", name="skill_publication_branch_epoch_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_publications.user_id",
                "skill_publications.account_id",
                "skill_publications.id",
            ],
            name="skill_publication_branch_attempt_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "state_id", "expected_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_publication_branch_head_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "state_id"],
            [
                "account_skill_states.user_id",
                "account_skill_states.account_id",
                "account_skill_states.id",
            ],
            name="skill_publication_branch_state_fk",
        ),
        sa.PrimaryKeyConstraint("publication_id", "state_id"),
    )


def downgrade() -> None:
    """
    存在发布或冲突历史时在任何修改之前拒绝丢弃恢复引用。
    """
    if op.get_bind().execute(sa.text("SELECT 1 FROM skill_publications LIMIT 1")).scalar():
        raise RuntimeError("publication history must be retained before downgrade")
    op.drop_table("skill_publication_branches")
    op.drop_table("skill_publications")
    op.drop_constraint("skill_finalization_owner_uq", "skill_finalizations", type_="unique")
