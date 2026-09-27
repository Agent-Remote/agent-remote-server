"""
为已授权的内容回收记录可重试删除任务，拒绝把未知旧标记解释为新授权。
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_skill_content_deletions"
down_revision = "0042_skill_tree_retention"
branch_labels = None
depends_on = None


def _require_no_markers() -> None:
    """
    未知 deleting 状态不能由升级或降级丢失其原始生命周期。
    """
    objects = sa.table("skill_content_objects", sa.column("status"))
    if (
        op.get_bind()
        .execute(
            sa.select(sa.literal(1))
            .select_from(objects)
            .where(objects.c.status == "deleting")
            .limit(1)
        )
        .first()
    ):
        raise RuntimeError("skill deletion markers must be preserved before schema change")


def upgrade() -> None:
    """
    只新增任务与必要约束，不回填旧对象、配额或磁盘内容。
    """
    _require_no_markers()
    op.create_table(
        "skill_content_deletions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("category_mask", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("size >= 0 AND attempts >= 0", name="skill_deletion_counts_ck"),
        sa.CheckConstraint("category_mask IN (1, 2, 3)", name="skill_deletion_categories_ck"),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL) OR "
            "(status = 'complete' AND completed_at IS NOT NULL)",
            name="skill_deletion_status_ck",
        ),
    )
    op.create_index(
        "skill_deletion_pending_uq",
        "skill_content_deletions",
        ["user_id", "digest"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "skill_deletion_due_idx", "skill_content_deletions", ["status", "next_attempt_at", "id"]
    )


def downgrade() -> None:
    """
    先完整拒绝有进度或未知标记的数据库，不能删除原任务身份后开放旧文件复用。
    """
    tasks = sa.table("skill_content_deletions", sa.column("id"))
    if op.get_bind().execute(sa.select(tasks.c.id).limit(1)).first():
        raise RuntimeError("skill deletion history must be preserved before downgrade")
    _require_no_markers()
    op.drop_table("skill_content_deletions")
