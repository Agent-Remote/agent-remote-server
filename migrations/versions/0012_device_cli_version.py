"""store the CLI version used by registered devices

Revision ID: 0012_device_cli_version
Revises: 0011_python_sync_excludes
Create Date: 2026-07-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_device_cli_version"
down_revision: str | None = "0011_python_sync_excludes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_devices", sa.Column("cli_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("user_devices", "cli_version")
