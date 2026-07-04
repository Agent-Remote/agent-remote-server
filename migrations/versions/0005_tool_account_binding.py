"""expand tool account binding states

Revision ID: 0005_tool_account_binding
Revises: 0004_connection_fields
Create Date: 2026-07-05 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_tool_account_binding"
down_revision: str | None = "0004_connection_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_STATES = (
    "'binding_requested', "
    "'binding_session_starting', "
    "'binding_waiting_user_login', "
    "'binding_verifying', "
    "'active', "
    "'expired', "
    "'disabled', "
    "'failed', "
    "'node_unavailable'"
)


def upgrade() -> None:
    op.drop_constraint("tool_accounts_status_ck", "tool_accounts", type_="check")
    op.create_check_constraint(
        "tool_accounts_status_ck",
        "tool_accounts",
        f"status in ({NEW_STATES})",
    )


def downgrade() -> None:
    op.drop_constraint("tool_accounts_status_ck", "tool_accounts", type_="check")
    op.create_check_constraint(
        "tool_accounts_status_ck",
        "tool_accounts",
        "status in ('pending', 'binding', 'active', 'disabled', 'failed')",
    )
