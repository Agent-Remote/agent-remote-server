"""Add connection fields to nodes.

Revision ID: 0004_connection_fields
Revises: 0003_node_control
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_connection_fields"
down_revision: str | None = "0003_node_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("wireguard_public_key", sa.Text(), nullable=True))
    op.add_column("nodes", sa.Column("wireguard_endpoint", sa.String(length=255), nullable=True))
    op.add_column("nodes", sa.Column("ssh_host", sa.String(length=255), nullable=True))
    op.add_column("nodes", sa.Column("ssh_port", sa.Integer(), nullable=True))
    op.add_column("nodes", sa.Column("ssh_user", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "ssh_user")
    op.drop_column("nodes", "ssh_port")
    op.drop_column("nodes", "ssh_host")
    op.drop_column("nodes", "wireguard_endpoint")
    op.drop_column("nodes", "wireguard_public_key")
