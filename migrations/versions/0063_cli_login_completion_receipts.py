"""
为 CLI 登录保存按原始键恢复的加密完成回执。
"""

import sqlalchemy as sa
from alembic import op

revision = "0063_cli_login_receipts"
down_revision = "0062_device_reg_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    添加 CLI 登录完成回执字段。
    """
    """Add only encrypted receipt material and a unique recovery key."""

    op.add_column(
        "cli_login_codes",
        sa.Column("completion_request_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "cli_login_codes",
        sa.Column("encrypted_completion_receipt", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "cli_login_codes",
        sa.Column("completion_receipt_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "cli_login_codes_completion_key_uidx",
        "cli_login_codes",
        ["completion_request_id"],
        unique=True,
    )


def downgrade() -> None:
    """
    移除 CLI 登录完成回执字段。
    """
    """Refuse to discard receipts that may still be needed for recovery."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE cli_login_codes IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM cli_login_codes WHERE encrypted_completion_receipt IS NOT NULL"
        )
    ):
        raise RuntimeError("cannot downgrade CLI login completion receipts after use")
    op.drop_index("cli_login_codes_completion_key_uidx", table_name="cli_login_codes")
    op.drop_column("cli_login_codes", "completion_receipt_expires_at")
    op.drop_column("cli_login_codes", "encrypted_completion_receipt")
    op.drop_column("cli_login_codes", "completion_request_id")
