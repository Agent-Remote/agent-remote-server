"""
增加可轮换的 CLI 登录会话。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_cli_login_sessions"
down_revision: str | None = "0024_ego_origin_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    创建只保存刷新凭据哈希的会话表。
    """
    op.create_table(
        "cli_login_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "access_token_id",
            sa.Uuid(),
            sa.ForeignKey("auth_tokens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "cli_login_sessions_access_uidx", "cli_login_sessions", ["access_token_id"], unique=True
    )
    op.create_index(
        "cli_login_sessions_refresh_uidx", "cli_login_sessions", ["refresh_token_hash"], unique=True
    )
    op.add_column(
        "auth_tokens",
        sa.Column(
            "cli_session_id",
            sa.Uuid(),
            sa.ForeignKey(
                "cli_login_sessions.id", name="auth_tokens_cli_session_fk", ondelete="SET NULL"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """
    删除长期会话，保留短期访问令牌。
    """
    op.drop_column("auth_tokens", "cli_session_id")
    op.drop_table("cli_login_sessions")
