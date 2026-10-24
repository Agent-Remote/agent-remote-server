"""
新增设备登记调用方原键及加密回执表。
"""

import sqlalchemy as sa
from alembic import op

revision = "0062_device_reg_requests"
down_revision = "0061_port_forward_caller_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    保存设备登记意图摘要和短期加密令牌回执。
    """

    op.create_table(
        "device_registration_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("intent_fingerprint", sa.String(71), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("ssh_key_id", sa.Uuid(), nullable=True),
        sa.Column("wireguard_peer_id", sa.Uuid(), nullable=True),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("encrypted_device_token", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("phase", sa.String(32), nullable=False, server_default="pending"),
        sa.UniqueConstraint(
            "user_id", "request_id", name="device_registration_requests_user_key_uq"
        ),
        sa.CheckConstraint(
            "length(intent_fingerprint) = 71", name="device_registration_requests_digest_ck"
        ),
    )
    op.create_index(
        "device_registration_requests_device_idx", "device_registration_requests", ["device_id"]
    )


def downgrade() -> None:
    """
    仅允许在没有设备登记原键记录时回退。
    """

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("LOCK TABLE device_registration_requests IN ACCESS EXCLUSIVE MODE")
        )
    if connection.scalar(sa.text("SELECT count(*) FROM device_registration_requests")):
        raise RuntimeError("cannot downgrade device registration requests")
    op.drop_index(
        "device_registration_requests_device_idx", table_name="device_registration_requests"
    )
    op.drop_table("device_registration_requests")
