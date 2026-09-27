"""
为完整树记录真实释放时间，保留未知旧树与所有既有内容引用。
"""

import sqlalchemy as sa
from alembic import op

revision = "0042_skill_tree_retention"
down_revision = "0041_skill_history_retirement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    新列保持可空，不按创建时间或部署时间猜测既有树的释放事件。
    """
    op.add_column(
        "skill_stored_trees",
        sa.Column("retention_released_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """
    删除列前整体检查，拒绝丢失任何已记录的真实树等待时钟。
    """
    trees = sa.table("skill_stored_trees", sa.column("retention_released_at"))
    if (
        op.get_bind()
        .execute(
            sa.select(sa.literal(1))
            .select_from(trees)
            .where(trees.c.retention_released_at.is_not(None))
            .limit(1)
        )
        .first()
    ):
        raise RuntimeError("skill tree retention clocks must be preserved before downgrade")
    op.drop_column("skill_stored_trees", "retention_released_at")
