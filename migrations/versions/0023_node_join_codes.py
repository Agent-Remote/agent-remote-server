"""
添加 Node 加入码及 ego-browser 意图字段。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_node_join_codes"
down_revision: str | None = "0022_ego_browser_ensure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建一次性 Node 加入码记录并为节点补齐显式能力意图。
    """

    with op.batch_alter_table("nodes") as batch:
        batch.add_column(
            sa.Column(
                "ego_browser_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.alter_column("ego_browser_enabled", server_default=None)

    op.create_table(
        "node_join_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("issuer_user_id", sa.UUID(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("server_origin", sa.String(length=255), nullable=False),
        sa.Column("release_profile", sa.String(length=128), nullable=True),
        sa.Column("wrapper_version", sa.String(length=64), nullable=True),
        sa.Column("skill_version", sa.String(length=64), nullable=True),
        sa.Column("runtime_version", sa.String(length=64), nullable=True),
        sa.Column("artifact_digest", sa.String(length=128), nullable=True),
        sa.Column("profile_digest", sa.String(length=128), nullable=True),
        sa.Column("ego_browser_enabled", sa.Boolean(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exchange_id", sa.String(length=128), nullable=True),
        sa.Column("encrypted_node_token", sa.LargeBinary(), nullable=True),
        sa.Column("exchange_result_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issuer_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("node_join_codes_hash_uidx", "node_join_codes", ["code_hash"], unique=True)
    op.create_index(
        "node_join_codes_node_status_idx",
        "node_join_codes",
        ["node_id", "consumed_at", "revoked_at"],
    )
    op.create_index(
        "node_join_codes_exchange_uidx",
        "node_join_codes",
        ["exchange_id"],
        unique=True,
    )


def downgrade() -> None:
    """
    删除 Node 加入码记录及显式能力意图字段。
    """

    op.drop_index("node_join_codes_exchange_uidx", table_name="node_join_codes")
    op.drop_index("node_join_codes_node_status_idx", table_name="node_join_codes")
    op.drop_index("node_join_codes_hash_uidx", table_name="node_join_codes")
    op.drop_table("node_join_codes")
    with op.batch_alter_table("nodes") as batch:
        batch.drop_column("ego_browser_enabled")
