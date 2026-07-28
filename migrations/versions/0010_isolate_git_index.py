"""isolate workspace git index

Revision ID: 0010_isolate_git_index
Revises: 0009_windows_device_platform
Create Date: 2026-07-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_isolate_git_index"
down_revision: str | None = "0009_windows_device_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为既有同步会话追加 Git index 排除规则。"""

    op.execute(
        """
        UPDATE sync_sessions
        SET exclude_patterns = (
            exclude_patterns::jsonb || '[".git/index"]'::jsonb
        )::json
        WHERE NOT (
            exclude_patterns::jsonb @> '[".git/index"]'::jsonb
        )
        """
    )


def downgrade() -> None:
    """从既有同步会话移除 Git index 排除规则。"""

    op.execute(
        """
        UPDATE sync_sessions
        SET exclude_patterns = (
            SELECT COALESCE(jsonb_agg(value), '[]'::jsonb)
            FROM jsonb_array_elements_text(exclude_patterns::jsonb) AS value
            WHERE value <> '.git/index'
        )::json
        """
    )
