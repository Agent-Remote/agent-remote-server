"""Add explicit device-session authorization metadata.

Revision ID: 0017_device_authorization
Revises: 0016_device_binding_rebind
Create Date: 2026-09-03 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_device_authorization"
down_revision: str | None = "0016_device_binding_rebind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加显式授权字段并把全部历史记录回填为逐应用兼容模式。"""

    op.add_column(
        "device_sessions",
        sa.Column("authorization_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "device_sessions",
        sa.Column("authorization_policy_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "device_sessions",
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE device_sessions "
            "SET authorization_mode = 'per_application_approval', "
            "authorization_policy_version = 1"
        )
    )
    op.alter_column("device_sessions", "authorization_mode", nullable=False)
    op.alter_column("device_sessions", "authorization_policy_version", nullable=False)
    op.create_check_constraint(
        "device_sessions_authorization_mode_ck",
        "device_sessions",
        "authorization_mode in ('per_application_approval', 'session_full_trust')",
    )
    op.create_check_constraint(
        "device_sessions_authorization_policy_version_ck",
        "device_sessions",
        "authorization_policy_version = 1",
    )
    op.create_check_constraint(
        "device_sessions_full_trust_authorized_at_ck",
        "device_sessions",
        "(authorization_mode = 'session_full_trust' and authorized_at is not null) or "
        "(authorization_mode = 'per_application_approval' and authorized_at is null)",
    )


def downgrade() -> None:
    """删除授权元数据并恢复 0016 的隐式逐应用语义。"""

    op.drop_constraint(
        "device_sessions_full_trust_authorized_at_ck",
        "device_sessions",
        type_="check",
    )
    op.drop_constraint(
        "device_sessions_authorization_policy_version_ck",
        "device_sessions",
        type_="check",
    )
    op.drop_constraint(
        "device_sessions_authorization_mode_ck",
        "device_sessions",
        type_="check",
    )
    op.drop_column("device_sessions", "authorized_at")
    op.drop_column("device_sessions", "authorization_policy_version")
    op.drop_column("device_sessions", "authorization_mode")
