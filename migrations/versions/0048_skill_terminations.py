"""
保留精确会话终止观察，不回填未知历史或改变用户删除策略。
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_skill_terminations"
down_revision = "0047_skill_deployment_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    独立收据按快照唯一，不将上传对象登记为已持久化内容。
    """
    op.create_table(
        "skill_snapshot_terminations",
        sa.Column("snapshot_id", sa.Uuid(), primary_key=True),
        sa.Column("incoming_digest", sa.String(64), nullable=False),
        sa.Column("unclean", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["session_skill_snapshots.id"]),
        sa.CheckConstraint("length(incoming_digest) = 64", name="skill_termination_digest_ck"),
    )


def downgrade() -> None:
    """
    拒绝删除已记录终止依据的降级。
    """
    op.execute(sa.text("LOCK TABLE skill_snapshot_terminations IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM skill_snapshot_terminations")):
        raise RuntimeError("cannot downgrade recorded skill terminations")
    op.drop_table("skill_snapshot_terminations")
