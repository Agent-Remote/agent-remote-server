"""add node control fields

Revision ID: 0003_node_control
Revises: 0002_identity_auth
Create Date: 2026-07-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_node_control"
down_revision: str | None = "0002_identity_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """
    添加节点控制字段
    """

    op.add_column("nodes", sa.Column("registration_token_hash", sa.Text(), nullable=True))
    op.add_column("nodes", sa.Column("node_token_hash", sa.Text(), nullable=True))
    op.add_column("nodes", sa.Column("last_heartbeat_at", TIMESTAMPTZ, nullable=True))
    op.add_column("nodes", sa.Column("version", sa.String(length=64), nullable=True))
    op.create_index(
        "nodes_registration_token_hash_uidx", "nodes", ["registration_token_hash"], unique=True
    )
    op.create_index("nodes_node_token_hash_uidx", "nodes", ["node_token_hash"], unique=True)
    op.create_index("nodes_status_heartbeat_idx", "nodes", ["status", "last_heartbeat_at"])


def downgrade() -> None:
    """
    删除节点控制字段
    """

    op.drop_index("nodes_status_heartbeat_idx", table_name="nodes")
    op.drop_index("nodes_node_token_hash_uidx", table_name="nodes")
    op.drop_index("nodes_registration_token_hash_uidx", table_name="nodes")
    op.drop_column("nodes", "version")
    op.drop_column("nodes", "last_heartbeat_at")
    op.drop_column("nodes", "node_token_hash")
    op.drop_column("nodes", "registration_token_hash")
