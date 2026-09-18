"""
添加 ego-browser ensure 幂等记录。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_ego_browser_ensure"
down_revision: str | None = "0021_ego_browser_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    保存 ensure 请求指纹和受保护的短期响应恢复材料。
    """

    op.create_table(
        "ego_browser_ensure_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("ego_browser_device_id", sa.UUID(), nullable=False),
        sa.Column("logical_operation", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("credential_id", sa.UUID(), nullable=True),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=True),
        sa.Column("credential_revision", sa.BigInteger(), nullable=True),
        sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["ego_browser_device_id"], ["ego_browser_devices.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"], ["ego_browser_device_credentials.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "logical_operation",
            "idempotency_key_hash",
            name="ego_browser_ensure_requests_key_uidx",
        ),
    )
    op.create_index(
        "ego_browser_ensure_requests_device_idx",
        "ego_browser_ensure_requests",
        ["ego_browser_device_id", "created_at"],
    )


def downgrade() -> None:
    """
    删除 ensure 幂等记录。
    """

    op.drop_index(
        "ego_browser_ensure_requests_device_idx", table_name="ego_browser_ensure_requests"
    )
    op.drop_table("ego_browser_ensure_requests")
