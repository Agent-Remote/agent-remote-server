"""
允许现有后端迁移服务持久化迁移中账户，并在降级时保留未完成状态。
"""

import sqlalchemy as sa
from alembic import op

revision = "0056_runtime_migration_status"
down_revision = "0055_skill_object_availability"
branch_labels = None
depends_on = None

PREVIOUS_STATES = (
    "'binding_requested', 'binding_session_starting', 'binding_waiting_user_login', "
    "'binding_verifying', 'active', 'expired', 'disabled', 'failed', 'node_unavailable'"
)


def upgrade() -> None:
    """
    扩展原约束，不改变任何账户内容、归属、迁移任务或原始结果。
    """
    op.drop_constraint("tool_accounts_status_ck", "tool_accounts", type_="check")
    op.create_check_constraint(
        "tool_accounts_status_ck", "tool_accounts", f"status in ({PREVIOUS_STATES}, 'migrating')"
    )


def downgrade() -> None:
    """
    锁定账户表后拒绝尚在迁移的行，避免降级抹去需要恢复的状态。
    """
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE tool_accounts IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM tool_accounts WHERE status = 'migrating')")
    ):
        raise RuntimeError("cannot downgrade while tool accounts are migrating")
    op.drop_constraint("tool_accounts_status_ck", "tool_accounts", type_="check")
    op.create_check_constraint(
        "tool_accounts_status_ck", "tool_accounts", f"status in ({PREVIOUS_STATES})"
    )
