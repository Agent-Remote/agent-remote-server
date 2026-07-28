"""exclude generated python state

Revision ID: 0011_exclude_generated_python_state
Revises: 0010_isolate_git_index
Create Date: 2026-07-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_exclude_generated_python_state"
down_revision: str | None = "0010_isolate_git_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GENERATED_PYTHON_EXCLUDES = [
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".coverage",
    ".coverage.*",
    "htmlcov",
]


def upgrade() -> None:
    """为既有同步会话追加 Python 生成状态排除规则。"""

    for pattern in GENERATED_PYTHON_EXCLUDES:
        escaped = pattern.replace("'", "''")
        op.execute(
            f"""
            UPDATE sync_sessions
            SET exclude_patterns = (
                exclude_patterns::jsonb || '["{escaped}"]'::jsonb
            )::json
            WHERE NOT (
                exclude_patterns::jsonb @> '["{escaped}"]'::jsonb
            )
            """
        )


def downgrade() -> None:
    """从既有同步会话移除 Python 生成状态排除规则。"""

    values = ", ".join(f"'{pattern}'" for pattern in GENERATED_PYTHON_EXCLUDES)
    op.execute(
        f"""
        UPDATE sync_sessions
        SET exclude_patterns = (
            SELECT COALESCE(jsonb_agg(value), '[]'::jsonb)
            FROM jsonb_array_elements_text(exclude_patterns::jsonb) AS value
            WHERE value NOT IN ({values})
        )::json
        """
    )
