"""
记录完整收尾内容首次持久化时间，不猜测旧记录的同步历史。
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_skill_sync_time"
down_revision = "0045_skill_prune_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    旧记录保留空值，上传未完成时不能拥有完成时间。
    """
    op.add_column(
        "skill_finalizations", sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "skill_finalization_persisted_time_ck",
        "skill_finalizations",
        "persisted_at IS NULL OR status <> 'upload_pending'",
    )


def downgrade() -> None:
    """
    有真实同步证据时先拒绝，避免降级静默删除不可重建的时间。
    """
    op.execute(sa.text("LOCK TABLE skill_finalizations IN ACCESS EXCLUSIVE MODE"))
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM skill_finalizations WHERE persisted_at IS NOT NULL")
    )
    if count:
        raise RuntimeError("cannot downgrade recorded skill synchronization times")
    op.drop_constraint("skill_finalization_persisted_time_ck", "skill_finalizations", type_="check")
    op.drop_column("skill_finalizations", "persisted_at")
