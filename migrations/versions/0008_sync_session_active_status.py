"""align runtime session status constraints

Revision ID: 0008_sync_session_active_status
Revises: 0007_native_runtime
Create Date: 2026-07-23 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_sync_session_active_status"
down_revision: str | None = "0007_native_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("sync_sessions_status_ck", "sync_sessions", type_="check")
    op.create_check_constraint(
        "sync_sessions_status_ck",
        "sync_sessions",
        "status in ('starting', 'active', 'healthy', 'paused', 'conflicted', 'failed', 'stopped')",
    )
    op.drop_constraint("sessions_status_ck", "sessions", type_="check")
    op.create_check_constraint(
        "sessions_status_ck",
        "sessions",
        "status in ('starting', 'running', 'active', 'detached', 'interrupted', "
        "'stopping', 'stopped', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("sessions_status_ck", "sessions", type_="check")
    op.create_check_constraint(
        "sessions_status_ck",
        "sessions",
        "status in ('starting', 'active', 'detached', 'stopping', 'stopped', 'failed')",
    )
    op.drop_constraint("sync_sessions_status_ck", "sync_sessions", type_="check")
    op.create_check_constraint(
        "sync_sessions_status_ck",
        "sync_sessions",
        "status in ('starting', 'healthy', 'paused', 'conflicted', 'failed', 'stopped')",
    )
