"""记录工具会话启动时注入的设备控制协议

Revision ID: 0015_session_device_control
Revises: 0014_device_sessions
Create Date: 2026-07-30 20:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_session_device_control"
down_revision: str | None = "0014_device_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    增加工具会话设备控制协议版本
    """

    op.add_column(
        "sessions",
        sa.Column("device_control_protocol_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "sessions_device_control_protocol_ck",
        "sessions",
        "device_control_protocol_version IS NULL OR device_control_protocol_version = 1",
    )


def downgrade() -> None:
    """
    删除工具会话设备控制协议版本
    """

    op.drop_constraint(
        "sessions_device_control_protocol_ck",
        "sessions",
        type_="check",
    )
    op.drop_column("sessions", "device_control_protocol_version")
