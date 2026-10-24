"""
新增不可因丢失 HTTP 响应而重复派发的无内容导入受理凭据。
"""

import sqlalchemy as sa
from alembic import op

revision = "0057_config_import_requests"
down_revision = "0056_runtime_migration_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    调用方键与任务绑定由数据库唯一约束兜底，不复制原始文件正文。
    """
    op.create_table(
        "tool_account_config_import_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("tool_account_id", sa.Uuid(), nullable=False),
        sa.Column("tool_type", sa.String(32), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("task_record_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.String(256), nullable=False),
        sa.Column("intent_fingerprint", sa.String(71), nullable=False),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("user_id", "request_id", name="config_import_requests_user_key_uq"),
        sa.UniqueConstraint("task_record_id", name="config_import_requests_task_uq"),
        sa.CheckConstraint(
            "length(intent_fingerprint) = 71", name="config_import_requests_digest_ck"
        ),
    )


def downgrade() -> None:
    """
    非空凭据不能降级删除，否则旧调用方键可能被误认为从未接受。
    """
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("LOCK TABLE tool_account_config_import_requests IN ACCESS EXCLUSIVE MODE")
        )
    if connection.scalar(sa.text("SELECT count(*) FROM tool_account_config_import_requests")):
        raise RuntimeError("cannot downgrade accepted config import requests")
    op.drop_table("tool_account_config_import_requests")
