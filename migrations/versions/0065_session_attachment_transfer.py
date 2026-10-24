"""
保存精确会话附件传输租约和清理回执。
"""

import sqlalchemy as sa
from alembic import op

revision = "0065_session_attachment_transfer"
down_revision = "0064_wireguard_peer_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    建立不依赖资源删除的身份快照和清理回执表。
    """

    op.create_table(
        "session_attachment_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("ssh_key_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_backend", sa.String(32), nullable=False),
        sa.Column("runtime_resource_id", sa.String(128), nullable=True),
        sa.Column("runtime_scope", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="issued"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_entries", sa.Integer(), nullable=False, server_default="32"),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_entries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.String(64), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('issued', 'cleanup_pending', 'cleaned', 'expired', 'revoked', 'rejected')",
            name="session_attachment_leases_status_ck",
        ),
        sa.CheckConstraint("max_bytes > 0", name="session_attachment_leases_max_bytes_ck"),
        sa.CheckConstraint(
            "max_entries between 1 and 32", name="session_attachment_leases_max_entries_ck"
        ),
        sa.CheckConstraint(
            "used_bytes >= 0 and used_entries >= 0",
            name="session_attachment_leases_usage_ck",
        ),
    )
    op.create_index(
        "session_attachment_leases_session_idx",
        "session_attachment_leases",
        ["session_id", "status"],
    )
    op.create_index(
        "session_attachment_leases_device_idx",
        "session_attachment_leases",
        ["device_id", "status"],
    )
    op.create_index(
        "session_attachment_leases_expires_idx",
        "session_attachment_leases",
        ["status", "expires_at"],
    )
    op.create_table(
        "session_attachment_cleanup_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("ssh_key_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("removed_batches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("lease_id", name="session_attachment_receipts_lease_uidx"),
        sa.UniqueConstraint("receipt_id", name="session_attachment_receipts_receipt_uidx"),
        sa.CheckConstraint(
            "status in ('cleaned', 'pending', 'unknown', 'rejected')",
            name="session_attachment_receipts_status_ck",
        ),
        sa.CheckConstraint(
            "removed_batches between 0 and 32",
            name="session_attachment_receipts_batches_ck",
        ),
    )
    op.create_index(
        "session_attachment_receipts_session_idx",
        "session_attachment_cleanup_receipts",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    """
    仅在没有租约和回执时允许回退，避免丢失未知清理结果。
    """

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE session_attachment_cleanup_receipts, session_attachment_leases "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    if connection.scalar(sa.text("SELECT count(*) FROM session_attachment_cleanup_receipts")):
        raise RuntimeError("cannot downgrade after attachment cleanup receipts exist")
    if connection.scalar(sa.text("SELECT count(*) FROM session_attachment_leases")):
        raise RuntimeError("cannot downgrade after attachment leases exist")
    op.drop_index(
        "session_attachment_receipts_session_idx",
        table_name="session_attachment_cleanup_receipts",
    )
    op.drop_table("session_attachment_cleanup_receipts")
    op.drop_index("session_attachment_leases_expires_idx", table_name="session_attachment_leases")
    op.drop_index("session_attachment_leases_device_idx", table_name="session_attachment_leases")
    op.drop_index("session_attachment_leases_session_idx", table_name="session_attachment_leases")
    op.drop_table("session_attachment_leases")
