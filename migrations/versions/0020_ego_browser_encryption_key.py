"""
添加可空的独立 X25519 公钥；旧设备重新登记密钥前不得激活或进入中继。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_ego_browser_encryption_key"
down_revision: str | None = "0019_ego_browser_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    保存 Bridge 的独立 X25519 公钥。
    """

    op.add_column(
        "ego_browser_devices",
        sa.Column("encryption_public_key", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """
    移除 Bridge 加密公钥列。
    """

    op.drop_column("ego_browser_devices", "encryption_public_key")
