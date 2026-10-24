"""
新增工具会话固定集合删除调用方原键受理凭据表。
"""

import sqlalchemy as sa
from alembic import op

revision = "0060_session_delete_requests"
down_revision = "0059_session_stop_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    保存用户确认的准确会话集合、筛选摘要和删除后仍可查询的回执。
    """
    op.create_table(
        "session_delete_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tool_type", sa.String(32), nullable=False),
        sa.Column("session_ids", sa.JSON(), nullable=False),
        sa.Column("selection_summary", sa.JSON(), nullable=False),
        sa.Column("selection_digest", sa.String(71), nullable=False),
        sa.Column("intent_fingerprint", sa.String(71), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False),
        sa.Column("acceptance_code", sa.String(96), nullable=True),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("observation_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("user_id", "request_id", name="session_delete_requests_user_key_uq"),
        sa.CheckConstraint(
            "length(selection_digest) = 71", name="session_delete_requests_selection_ck"
        ),
        sa.CheckConstraint(
            "length(intent_fingerprint) = 71", name="session_delete_requests_intent_ck"
        ),
    )
    op.create_index(
        "session_delete_requests_selection_idx",
        "session_delete_requests",
        ["user_id", "selection_digest"],
    )


def downgrade() -> None:
    """
    仅允许在没有固定集合删除受理凭据时删除，避免原键重放语义倒退。
    """
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE session_delete_requests IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM session_delete_requests")):
        raise RuntimeError("cannot downgrade accepted session delete requests")
    op.drop_index("session_delete_requests_selection_idx", table_name="session_delete_requests")
    op.drop_table("session_delete_requests")
