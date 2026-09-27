"""
分离进程停止与完整冻结证据，不将缺失摘要回填为虚构内容。
"""

import sqlalchemy as sa
from alembic import op

revision = "0053_skill_capture_pending"
down_revision = "0052_skill_deployment_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    保留既有冻结输入，允许严格受限的停止后捕获失败观察。
    """
    op.alter_column(
        "skill_snapshot_terminations", "incoming_digest", existing_type=sa.String(64), nullable=True
    )
    op.add_column(
        "skill_snapshot_terminations", sa.Column("capture_error", sa.String(32), nullable=True)
    )
    op.create_check_constraint(
        "skill_termination_capture_ck",
        "skill_snapshot_terminations",
        "(incoming_digest IS NOT NULL AND capture_error IS NULL) OR "
        "(incoming_digest IS NULL AND capture_error IS NOT NULL AND capture_error IN "
        "('quota_exceeded', 'insufficient_storage', 'portability_error', 'capture_failed'))",
    )


def downgrade() -> None:
    """
    原结构无法表示未冻结停止时拒绝降级，不删除恢复依据。
    """
    op.execute(sa.text("LOCK TABLE skill_snapshot_terminations IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM skill_snapshot_terminations WHERE incoming_digest IS NULL")
    ):
        raise RuntimeError("cannot downgrade pending skill capture observations")
    op.drop_constraint("skill_termination_capture_ck", "skill_snapshot_terminations", type_="check")
    op.drop_column("skill_snapshot_terminations", "capture_error")
    op.alter_column(
        "skill_snapshot_terminations",
        "incoming_digest",
        existing_type=sa.String(64),
        nullable=False,
    )
