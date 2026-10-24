"""
为 session 端口转发增加可重放的调用方原键。
"""

import sqlalchemy as sa
from alembic import op

revision = "0061_port_forward_caller_keys"
down_revision = "0060_session_delete_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    添加调用方请求键字段与索引。
    """
    """保存端口转发创建意图并按用户约束原键唯一。"""

    op.add_column(
        "port_forwards",
        sa.Column("caller_request_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "port_forwards",
        sa.Column("caller_intent_digest", sa.String(length=71), nullable=True),
    )
    # A unique index (rather than a table constraint) keeps SQLite migration
    # support and permits any number of legacy NULL caller keys.
    op.create_index(
        "port_forwards_user_caller_key_uq",
        "port_forwards",
        ["user_id", "caller_request_id"],
        unique=True,
    )


def downgrade() -> None:
    """
    移除端口转发调用方请求键字段与索引。
    """
    """删除原键字段；仅用于未产生 caller-key 记录的开发数据库。"""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE port_forwards IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text("SELECT count(*) FROM port_forwards WHERE caller_request_id IS NOT NULL")
    ):
        raise RuntimeError("cannot downgrade port forward caller keys after use")
    op.drop_index("port_forwards_user_caller_key_uq", table_name="port_forwards")
    op.drop_column("port_forwards", "caller_intent_digest")
    op.drop_column("port_forwards", "caller_request_id")
