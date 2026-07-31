"""新增本地设备控制会话

Revision ID: 0014_device_sessions
Revises: 0013_session_port_forwards
Create Date: 2026-07-30 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_device_sessions"
down_revision: str | None = "0013_session_port_forwards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """
    创建设备控制会话与本机审批摘要表
    """

    op.create_table(
        "device_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "device_id", UUID, sa.ForeignKey("user_devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "tool_session_id",
            UUID,
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("lease_until", TIMESTAMPTZ, nullable=True),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("lock_acquired_at", TIMESTAMPTZ, nullable=True),
        sa.Column("stopped_at", TIMESTAMPTZ, nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("platform = 'macos'", name="device_sessions_platform_ck"),
        sa.CheckConstraint(
            "generation between 1 and 9223372036854775807",
            name="device_sessions_generation_ck",
        ),
        sa.CheckConstraint(
            "generation <= 9223372036854775806 or "
            "status in ('stopped', 'denied', 'expired', 'failed')",
            name="device_sessions_active_generation_ck",
        ),
        sa.CheckConstraint(
            "status in ('pending_device', 'pending_user_approval', 'active', 'stopping', "
            "'stopped', 'denied', 'expired', 'failed')",
            name="device_sessions_status_ck",
        ),
    )
    op.create_index(
        "device_sessions_user_status_idx", "device_sessions", ["user_id", "status", "created_at"]
    )
    op.create_index(
        "device_sessions_device_status_idx",
        "device_sessions",
        ["device_id", "status", "created_at"],
    )
    op.create_index(
        "device_sessions_tool_uidx", "device_sessions", ["tool_session_id"], unique=True
    )
    op.create_index("device_sessions_lease_idx", "device_sessions", ["status", "lease_until"])
    op.create_index(
        "device_sessions_machine_lock_uidx",
        "device_sessions",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("lock_acquired_at IS NOT NULL"),
        sqlite_where=sa.text("lock_acquired_at IS NOT NULL"),
    )

    op.create_table(
        "device_session_approvals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "device_session_id",
            UUID,
            sa.ForeignKey("device_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("application_digest", sa.String(length=64), nullable=False),
        sa.Column("control_level", sa.String(length=32), nullable=False),
        sa.Column("approval_result", sa.String(length=32), nullable=False),
        sa.Column("clipboard_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("audit_correlation_id", UUID, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "control_level in ('view_only', 'click_only', 'full_control')",
            name="device_session_approvals_level_ck",
        ),
        sa.CheckConstraint(
            "approval_result in ('allowed', 'denied')", name="device_session_approvals_result_ck"
        ),
        sa.CheckConstraint(
            "length(application_digest) = 64", name="device_session_approvals_digest_ck"
        ),
    )
    op.create_index(
        "device_session_approvals_session_app_uidx",
        "device_session_approvals",
        ["device_session_id", "application_digest"],
        unique=True,
    )


def downgrade() -> None:
    """
    删除设备控制会话与本机审批摘要表
    """

    op.drop_index(
        "device_session_approvals_session_app_uidx", table_name="device_session_approvals"
    )
    op.drop_table("device_session_approvals")
    op.drop_index("device_sessions_machine_lock_uidx", table_name="device_sessions")
    op.drop_index("device_sessions_lease_idx", table_name="device_sessions")
    op.drop_index("device_sessions_tool_uidx", table_name="device_sessions")
    op.drop_index("device_sessions_device_status_idx", table_name="device_sessions")
    op.drop_index("device_sessions_user_status_idx", table_name="device_sessions")
    op.drop_table("device_sessions")
