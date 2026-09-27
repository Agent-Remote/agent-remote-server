"""
增加独立于原内容的 prune 回执、全部披露和实际删除任务关联，不回填历史。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_skill_prune_operations"
down_revision = "0044_skill_prune_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    只创建空元数据表和辅助唯一约束，既有历史、额度和删除任务保持原值。
    """
    op.create_unique_constraint(
        "skill_deletion_owner_uq", "skill_content_deletions", ["user_id", "id"]
    )
    op.create_table(
        "skill_prune_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_prune_operation_key_uq"),
        sa.UniqueConstraint("user_id", "id", name="skill_prune_operation_owner_uq"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_prune_operation_account_fk",
        ),
    )
    op.create_table(
        "skill_prune_operation_entries",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("ordinal", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("disclosure_json", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="skill_prune_entry_ordinal_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_prune_operations.user_id", "skill_prune_operations.id"],
            name="skill_prune_entry_operation_fk",
        ),
    )
    op.create_table(
        "skill_prune_operation_deletions",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("deletion_id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_prune_operations.user_id", "skill_prune_operations.id"],
            name="skill_prune_deletion_operation_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "deletion_id"],
            ["skill_content_deletions.user_id", "skill_content_deletions.id"],
            name="skill_prune_deletion_task_fk",
        ),
    )


def downgrade() -> None:
    """
    任何原受理存在时先拒绝，再按外键顺序移除空表和辅助约束。
    """
    operations = sa.table("skill_prune_operations", sa.column("id"))
    if op.get_bind().execute(sa.select(operations.c.id).limit(1)).first():
        raise RuntimeError("skill prune operation history must be preserved before downgrade")
    op.drop_table("skill_prune_operation_deletions")
    op.drop_table("skill_prune_operation_entries")
    op.drop_table("skill_prune_operations")
    op.drop_constraint("skill_deletion_owner_uq", "skill_content_deletions", type_="unique")
