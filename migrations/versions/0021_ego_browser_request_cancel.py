"""添加 request 级 ego-browser 取消账本状态。

Revision ID: 0021_ego_browser_cancel
Revises: 0020_ego_browser_encryption_key
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_ego_browser_cancel"
down_revision: str | None = "0020_ego_browser_encryption_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许持久化 request 取消状态转换。"""

    op.drop_constraint(
        "ego_browser_request_ledger_type_ck",
        "ego_browser_request_ledger",
        type_="check",
    )
    op.drop_constraint(
        "ego_browser_request_ledger_status_ck",
        "ego_browser_request_ledger",
        type_="check",
    )
    op.create_check_constraint(
        "ego_browser_request_ledger_type_ck",
        "ego_browser_request_ledger",
        "message_type in ('execute', 'execute_result', 'cancel')",
    )
    op.create_check_constraint(
        "ego_browser_request_ledger_status_ck",
        "ego_browser_request_ledger",
        "status in ('accepted', 'cancel_requested', 'cancelled', 'completed', 'rejected')",
    )


def downgrade() -> None:
    """恢复引入取消状态前的 request 账本约束。"""

    op.execute(
        "UPDATE ego_browser_request_ledger SET status = 'rejected' "
        "WHERE status IN ('cancel_requested', 'cancelled')"
    )
    op.execute(
        "UPDATE ego_browser_request_ledger SET message_type = 'execute' "
        "WHERE message_type = 'cancel'"
    )
    op.drop_constraint(
        "ego_browser_request_ledger_status_ck",
        "ego_browser_request_ledger",
        type_="check",
    )
    op.drop_constraint(
        "ego_browser_request_ledger_type_ck",
        "ego_browser_request_ledger",
        type_="check",
    )
    op.create_check_constraint(
        "ego_browser_request_ledger_status_ck",
        "ego_browser_request_ledger",
        "status in ('accepted', 'completed', 'rejected')",
    )
    op.create_check_constraint(
        "ego_browser_request_ledger_type_ck",
        "ego_browser_request_ledger",
        "message_type in ('execute', 'execute_result')",
    )
