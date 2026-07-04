"""add identity auth tables

Revision ID: 0002_identity_auth
Revises: 0001_core_schema
Create Date: 2026-07-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_identity_auth"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)


def created_at_column() -> sa.Column:
    """
    创建 created_at 字段

    :return Column: created_at 字段
    """

    return sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()"))


def updated_at_column() -> sa.Column:
    """
    创建 updated_at 字段

    :return Column: updated_at 字段
    """

    return sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    """
    创建身份认证扩展表
    """

    op.add_column("users", sa.Column("encrypted_totp_secret", sa.LargeBinary(), nullable=True))

    op.create_table(
        "auth_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "user_device_id",
            UUID,
            sa.ForeignKey("user_devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint("token_type in ('user', 'device')", name="auth_tokens_type_ck"),
        sa.CheckConstraint("status in ('active', 'revoked')", name="auth_tokens_status_ck"),
    )
    op.create_index("auth_tokens_hash_uidx", "auth_tokens", ["token_hash"], unique=True)
    op.create_index(
        "auth_tokens_user_status_idx", "auth_tokens", ["user_id", "status", "expires_at"]
    )
    op.create_index(
        "auth_tokens_device_status_idx",
        "auth_tokens",
        ["user_device_id", "status"],
    )

    op.create_table(
        "cli_login_codes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("device_code_hash", sa.Text(), nullable=False),
        sa.Column("user_code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "approved_user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("consumed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="5"),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'consumed', 'expired')",
            name="cli_login_codes_status_ck",
        ),
    )
    op.create_index(
        "cli_login_codes_device_hash_uidx",
        "cli_login_codes",
        ["device_code_hash"],
        unique=True,
    )
    op.create_index("cli_login_codes_user_code_uidx", "cli_login_codes", ["user_code"], unique=True)
    op.create_index("cli_login_codes_status_idx", "cli_login_codes", ["status", "expires_at"])


def downgrade() -> None:
    """
    删除身份认证扩展表
    """

    op.drop_index("cli_login_codes_status_idx", table_name="cli_login_codes")
    op.drop_index("cli_login_codes_user_code_uidx", table_name="cli_login_codes")
    op.drop_index("cli_login_codes_device_hash_uidx", table_name="cli_login_codes")
    op.drop_table("cli_login_codes")
    op.drop_index("auth_tokens_device_status_idx", table_name="auth_tokens")
    op.drop_index("auth_tokens_user_status_idx", table_name="auth_tokens")
    op.drop_index("auth_tokens_hash_uidx", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_column("users", "encrypted_totp_secret")
