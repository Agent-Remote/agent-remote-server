"""
新增原部署尝试的永久撤权意图，排空和终态回执仍由独立确认提交。
"""

import sqlalchemy as sa
from alembic import op

revision = "0051_skill_deployment_drains"
down_revision = "0050_skill_deployment_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    只新增空表与精确尝试外键，不推断既有任务的撤权或排空。
    """
    op.create_table(
        "skill_deployment_terminations",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_attempt", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["skill_deployment_tasks.attempt_id"]),
        sa.UniqueConstraint("id", name="skill_deployment_termination_id_uq"),
        sa.CheckConstraint(
            "lease_attempt >= 1 AND lease_attempt <= 2147483647",
            name="skill_deployment_termination_poll_ck",
        ),
        sa.CheckConstraint(
            "outcome IN ('failed', 'superseded')", name="skill_deployment_termination_outcome_ck"
        ),
        sa.CheckConstraint(
            "error_code IN ('NODE_UNAVAILABLE', 'TRANSFER_FAILED', 'QUOTA_EXCEEDED', "
            "'DEPLOYMENT_INTERRUPTED', 'AUTHORIZATION_DENIED', 'SKILL_MANAGER_UNSUPPORTED', "
            "'DEPLOYMENT_INPUT_INVALID', 'OPERATION_SUPERSEDED')",
            name="skill_deployment_termination_error_ck",
        ),
        sa.CheckConstraint(
            "error_code != 'OPERATION_SUPERSEDED' OR outcome = 'superseded'",
            name="skill_deployment_termination_replacement_ck",
        ),
    )


def downgrade() -> None:
    """
    先锁定并拒绝仍有撤权历史的降级，不能重新授权迟到的准备任务。
    """
    op.execute(sa.text("LOCK TABLE skill_deployment_terminations IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM skill_deployment_terminations")):
        raise RuntimeError("cannot downgrade recorded skill deployment termination intents")
    op.drop_table("skill_deployment_terminations")
