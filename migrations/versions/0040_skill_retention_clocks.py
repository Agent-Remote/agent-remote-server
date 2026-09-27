"""
新增真实引用释放时钟，既有未知历史不按部署时间回填。
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_skill_retention_clocks"
down_revision = "0039_skill_account_takeover"
branch_labels = None
depends_on = None

_TABLES = (
    "skill_revisions",
    "account_local_skill_revisions",
    "skill_checkpoints",
    "session_skill_snapshots",
    "skill_finalizations",
    "skill_publications",
    "skill_branch_preparations",
)


def upgrade() -> None:
    """
    可空列保持旧身份和内容不变，时钟由业务引用事务维护。
    """
    for table in _TABLES:
        op.add_column(
            table, sa.Column("retention_released_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """
    全部预检通过后才删除列，禁止部分降级或丢失真实释放证据。
    """
    for table in _TABLES:
        history = sa.table(table, sa.column("retention_released_at"))
        if (
            op.get_bind()
            .execute(
                sa.select(sa.literal(1))
                .select_from(history)
                .where(history.c.retention_released_at.is_not(None))
                .limit(1)
            )
            .first()
        ):
            raise RuntimeError("skill retention clocks must be preserved before downgrade")
    for table in reversed(_TABLES):
        op.drop_column(table, "retention_released_at")
