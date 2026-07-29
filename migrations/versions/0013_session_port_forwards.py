"""add session port forwards

Revision ID: 0013_session_port_forwards
Revises: 0012_device_cli_version
Create Date: 2026-07-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_session_port_forwards"
down_revision: str | None = "0012_device_cli_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """创建 session 端口转发表。"""

    op.create_table(
        "port_forwards",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "device_id",
            UUID,
            sa.ForeignKey("user_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ssh_key_id", UUID, sa.ForeignKey("ssh_keys.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column(
            "session_id", UUID, sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("remote_port", sa.Integer(), nullable=False),
        sa.Column("requested_local_port", sa.Integer(), nullable=False),
        sa.Column("client_instance_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("policy_snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("bytes_up", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_down", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("connection_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("connection_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation_bytes_up", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("generation_bytes_down", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "generation_connection_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("last_connected_at", TIMESTAMPTZ, nullable=True),
        sa.Column("lease_expires_at", TIMESTAMPTZ, nullable=True),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("stopped_at", TIMESTAMPTZ, nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("remote_port between 1 and 65535", name="port_forwards_remote_port_ck"),
        sa.CheckConstraint(
            "requested_local_port between 1 and 65535", name="port_forwards_local_port_ck"
        ),
        sa.CheckConstraint(
            "status in ('pending', 'active', 'disconnected', 'stopped', 'expired', "
            "'revoked', 'failed')",
            name="port_forwards_status_ck",
        ),
    )
    op.create_index(
        "port_forwards_user_status_idx", "port_forwards", ["user_id", "status", "created_at"]
    )
    op.create_index(
        "port_forwards_device_status_idx",
        "port_forwards",
        ["device_id", "status", "created_at"],
    )
    op.create_index(
        "port_forwards_session_status_idx",
        "port_forwards",
        ["session_id", "status", "created_at"],
    )
    op.create_index(
        "port_forwards_node_lease_idx",
        "port_forwards",
        ["node_id", "status", "lease_expires_at"],
    )


def downgrade() -> None:
    """删除 session 端口转发表。"""

    op.drop_index("port_forwards_node_lease_idx", table_name="port_forwards")
    op.drop_index("port_forwards_session_status_idx", table_name="port_forwards")
    op.drop_index("port_forwards_device_status_idx", table_name="port_forwards")
    op.drop_index("port_forwards_user_status_idx", table_name="port_forwards")
    op.drop_table("port_forwards")
