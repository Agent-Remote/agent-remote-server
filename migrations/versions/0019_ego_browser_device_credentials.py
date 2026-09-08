"""添加 ego-browser Device Client 独立短期凭据。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_ego_browser_credentials"
down_revision: str | None = "0018_ego_browser_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """创建独立 ego-browser Device Client 凭据表。"""

    op.create_table(
        "ego_browser_device_credentials",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ego_browser_device_id",
            UUID,
            sa.ForeignKey("ego_browser_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("credential_profile", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_used_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status in ('active', 'revoked', 'expired')",
            name="ego_browser_device_credentials_status_ck",
        ),
        sa.CheckConstraint(
            "credential_profile in ('community_file', 'keychain_access_group')",
            name="ego_browser_device_credentials_profile_ck",
        ),
        sa.CheckConstraint(
            "generation between 1 and 9223372036854775807",
            name="ego_browser_device_credentials_generation_ck",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ego_browser_device_credentials_revision_ck",
        ),
    )
    op.create_index(
        "ego_browser_device_credentials_hash_uidx",
        "ego_browser_device_credentials",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ego_browser_device_credentials_device_status_idx",
        "ego_browser_device_credentials",
        ["ego_browser_device_id", "status", "expires_at"],
    )
    op.create_index(
        "ego_browser_device_credentials_user_status_idx",
        "ego_browser_device_credentials",
        ["user_id", "status", "expires_at"],
    )


def downgrade() -> None:
    """删除独立 ego-browser Device Client 凭据表。"""

    op.drop_index(
        "ego_browser_device_credentials_user_status_idx",
        table_name="ego_browser_device_credentials",
    )
    op.drop_index(
        "ego_browser_device_credentials_device_status_idx",
        table_name="ego_browser_device_credentials",
    )
    op.drop_index(
        "ego_browser_device_credentials_hash_uidx",
        table_name="ego_browser_device_credentials",
    )
    op.drop_table("ego_browser_device_credentials")
