"""
追加原目标的部署尝试链与精确重试受理，不回填未知历史。
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_skill_deployment_attempts"
down_revision = "0048_skill_terminations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    新增历史身份约束，已有配置计划保持没有尝试版本的原始状态。
    """
    op.add_column("skill_operations", sa.Column("attempts_version", sa.Integer(), nullable=True))
    op.create_table(
        "skill_deployment_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("predecessor_id", sa.Uuid(), nullable=True),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.UniqueConstraint(
            "user_id", "operation_id", "account_id", "id", name="skill_attempt_owner_uq"
        ),
        sa.UniqueConstraint(
            "user_id", "operation_id", "account_id", "number", name="skill_attempt_number_uq"
        ),
        sa.UniqueConstraint(
            "user_id",
            "operation_id",
            "account_id",
            "predecessor_id",
            name="skill_attempt_successor_uq",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_targets.user_id",
                "skill_deployment_targets.operation_id",
                "skill_deployment_targets.account_id",
            ],
            name="skill_attempt_target_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id", "predecessor_id"],
            [
                "skill_deployment_attempts.user_id",
                "skill_deployment_attempts.operation_id",
                "skill_deployment_attempts.account_id",
                "skill_deployment_attempts.id",
            ],
            name="skill_attempt_predecessor_fk",
        ),
        sa.CheckConstraint(
            "number >= 1 AND ((number = 1 AND predecessor_id IS NULL) OR "
            "(number > 1 AND predecessor_id IS NOT NULL AND predecessor_id <> id))",
            name="skill_attempt_sequence_ck",
        ),
        sa.CheckConstraint(
            "status IN ('stored', 'unsupported', 'pending', 'running', 'ready', "
            "'needs_resolution', 'failed', 'superseded')",
            name="skill_attempt_status_ck",
        ),
        sa.CheckConstraint(
            "NOT retryable OR (status = 'failed' AND error_code IS NOT NULL AND "
            "error_code IN ('NODE_UNAVAILABLE', 'TRANSFER_FAILED', 'QUOTA_EXCEEDED', "
            "'DEPLOYMENT_INTERRUPTED'))",
            name="skill_attempt_retry_ck",
        ),
        sa.CheckConstraint("length(plan_digest) = 64", name="skill_attempt_digest_ck"),
        sa.CheckConstraint(
            "(status IN ('stored', 'pending', 'running', 'ready') AND error_code IS NULL) OR "
            "(status IN ('unsupported', 'needs_resolution', 'failed', 'superseded') "
            "AND error_code IS NOT NULL)",
            name="skill_attempt_error_ck",
        ),
    )
    op.create_table(
        "skill_deployment_retries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_deployment_retry_key_uq"),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_operations.user_id", "skill_operations.id"],
            name="skill_deployment_retry_operation_fk",
        ),
    )


def downgrade() -> None:
    """
    检查完整受理标记与历史后才删除空表，禁止丢弃真实重试身份。
    """
    op.execute(
        sa.text(
            "LOCK TABLE skill_operations, skill_deployment_attempts, "
            "skill_deployment_retries IN ACCESS EXCLUSIVE MODE"
        )
    )
    if any(
        op.get_bind().scalar(sa.text(query))
        for query in (
            "SELECT count(*) FROM skill_operations WHERE attempts_version IS NOT NULL",
            "SELECT count(*) FROM skill_deployment_attempts",
            "SELECT count(*) FROM skill_deployment_retries",
        )
    ):
        raise RuntimeError("cannot downgrade recorded skill deployment attempts")
    op.drop_table("skill_deployment_retries")
    op.drop_table("skill_deployment_attempts")
    op.drop_column("skill_operations", "attempts_version")
