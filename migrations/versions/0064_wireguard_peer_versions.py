"""
为 WireGuard peer 增加公开的并发版本。
"""

import sqlalchemy as sa
from alembic import op

revision = "0064_wireguard_peer_versions"
down_revision = "0063_cli_login_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    添加 WireGuard peer 版本字段。
    """
    """为既有 peer 初始化版本 1。"""

    op.add_column(
        "wireguard_peers",
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.alter_column("wireguard_peers", "version", server_default=None)


def downgrade() -> None:
    """
    移除 WireGuard peer 版本字段。
    """
    """回退前拒绝丢弃仍在使用的并发令牌。"""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE wireguard_peers IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM wireguard_peers")):
        raise RuntimeError("cannot downgrade while WireGuard peer identity versions exist")
    op.drop_column("wireguard_peers", "version")
