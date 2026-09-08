"""添加 ego-browser Bridge 使用的独立 X25519 公钥。

The column is intentionally nullable for rows created by migrations 0018/0019.
Service-layer activation and relay admission reject those legacy rows until the
device is explicitly re-registered with a key.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_ego_browser_encryption_key"
down_revision: str | None = "0019_ego_browser_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存 Bridge 的独立 X25519 公钥。"""

    op.add_column(
        "ego_browser_devices",
        sa.Column("encryption_public_key", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """移除 Bridge 加密公钥列。"""

    op.drop_column("ego_browser_devices", "encryption_public_key")
