"""allow Windows user devices

Revision ID: 0009_windows_device_platform
Revises: 0008_sync_session_active_status
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_windows_device_platform"
down_revision: str | None = "0008_sync_session_active_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("user_devices_platform_ck", "user_devices", type_="check")
    op.create_check_constraint(
        "user_devices_platform_ck",
        "user_devices",
        "platform in ('windows', 'macos', 'linux')",
    )


def downgrade() -> None:
    op.drop_constraint("user_devices_platform_ck", "user_devices", type_="check")
    op.create_check_constraint(
        "user_devices_platform_ck",
        "user_devices",
        "platform in ('macos', 'linux')",
    )
