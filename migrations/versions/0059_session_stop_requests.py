"""
新增工具会话停止调用方原键受理凭据表。
"""

import sqlalchemy as sa
from alembic import op

revision = "0059_session_stop_requests"
down_revision = "0058_session_create_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    保存无内容停止意图、原始任务身份和会话删除后的稳定观察入口。
    """
    op.create_table(
        "session_stop_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_backend", sa.String(32), nullable=False),
        sa.Column("runtime_resource_id", sa.String(128), nullable=True),
        sa.Column("task_record_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column("intent_fingerprint", sa.String(71), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False),
        sa.Column("acceptance_code", sa.String(96), nullable=True),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("observation_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("user_id", "request_id", name="session_stop_requests_user_key_uq"),
        sa.CheckConstraint(
            "length(intent_fingerprint) = 71", name="session_stop_requests_digest_ck"
        ),
    )
    op.create_index("session_stop_requests_session_idx", "session_stop_requests", ["session_id"])
    op.create_index("session_stop_requests_task_idx", "session_stop_requests", ["task_record_id"])


def downgrade() -> None:
    """
    仅在没有停止受理凭据时删除，避免调用方键重放语义倒退。
    """
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE session_stop_requests IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM session_stop_requests")):
        raise RuntimeError("cannot downgrade accepted session stop requests")
    op.drop_index("session_stop_requests_task_idx", table_name="session_stop_requests")
    op.drop_index("session_stop_requests_session_idx", table_name="session_stop_requests")
    op.drop_table("session_stop_requests")
