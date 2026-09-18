"""
持久化 ego-browser Server origin 并限制 ensure 交换材料保留期。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_ego_origin_retention"
down_revision: str | None = "0023_node_join_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    为设备身份绑定 origin，并记录 ensure 响应恢复材料的过期时间。
    """

    op.add_column(
        "ego_browser_devices",
        sa.Column("server_origin", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "ego_browser_ensure_requests",
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ego_browser_ensure_requests_expiry_idx",
        "ego_browser_ensure_requests",
        ["result_expires_at", "created_at"],
    )


def downgrade() -> None:
    """
    删除设备 origin 和 ensure 响应过期元数据。
    """

    op.drop_index(
        "ego_browser_ensure_requests_expiry_idx",
        table_name="ego_browser_ensure_requests",
    )
    op.drop_column("ego_browser_ensure_requests", "result_expires_at")
    op.drop_column("ego_browser_devices", "server_origin")
