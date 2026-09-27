"""
保留同账户的重置恢复结果与不可变幂等回执。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_skill_state_operations"
down_revision = "0032_skill_resolution_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    用完整归属和范围外键保护恢复输入和最终目录引用。
    """
    op.create_table(
        "skill_state_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("source_checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("result_scope", sa.String(16), nullable=False),
        sa.Column("result_checkpoint_id", sa.Uuid(), nullable=False),
        sa.Column("response_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_state_operation_key_uq"),
        sa.CheckConstraint(
            "action IN ('reset', 'restore')", name="skill_state_operation_action_ck"
        ),
        sa.CheckConstraint(
            "scope IN ('item', 'directory') AND result_scope = 'directory'",
            name="skill_state_operation_scope_ck",
        ),
        sa.CheckConstraint(
            "(action = 'reset' AND source_checkpoint_id IS NULL) OR "
            "(action = 'restore' AND source_checkpoint_id IS NOT NULL)",
            name="skill_state_operation_source_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "scope", "source_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_state_operation_source_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "result_scope", "result_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_state_operation_result_fk",
        ),
    )


def downgrade() -> None:
    """
    存在用户回执时拒绝删除幂等与恢复历史。
    """
    if op.get_bind().execute(sa.text("SELECT 1 FROM skill_state_operations LIMIT 1")).scalar():
        raise RuntimeError("state operation history must be retained before downgrade")
    op.drop_table("skill_state_operations")
