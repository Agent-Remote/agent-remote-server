"""
为后台准备绑定独立完整目录输入与原始尝试任务，不推断历史执行。
"""

import sqlalchemy as sa
from alembic import op

revision = "0050_skill_deployment_tasks"
down_revision = "0049_skill_deployment_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    通过复合外键阻止其他用户、账户、尝试、节点或内容替换。
    """
    op.create_table(
        "skill_deployment_tasks",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_scope", sa.String(16), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("task_id", name="skill_deployment_task_record_uq"),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id", "attempt_id"],
            [
                "skill_deployment_attempts.user_id",
                "skill_deployment_attempts.operation_id",
                "skill_deployment_attempts.account_id",
                "skill_deployment_attempts.id",
            ],
            name="skill_deployment_task_attempt_fk",
        ),
        sa.ForeignKeyConstraint(
            ["node_id", "task_id"],
            ["node_tasks.node_id", "node_tasks.id"],
            name="skill_deployment_task_node_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "checkpoint_id", "content_digest"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
                "skill_checkpoints.content_digest",
            ],
            name="skill_deployment_task_input_fk",
        ),
        sa.CheckConstraint("checkpoint_scope = 'directory'", name="skill_deployment_task_scope_ck"),
        sa.CheckConstraint("length(plan_digest) = 64", name="skill_deployment_task_plan_ck"),
    )


def downgrade() -> None:
    """
    原任务仍可重放时不能移除授权边界，检查先于任何破坏性修改。
    """
    op.execute(sa.text("LOCK TABLE skill_deployment_tasks IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM skill_deployment_tasks")):
        raise RuntimeError("cannot downgrade recorded skill deployment tasks")
    op.drop_table("skill_deployment_tasks")
