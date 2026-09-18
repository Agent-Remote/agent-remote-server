"""
允许替换 live 设备控制 binding。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_device_binding_rebind"
down_revision: str | None = "0015_session_device_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE_STATUS_SQL = "status NOT IN ('stopped', 'denied', 'expired', 'failed')"
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    """
    仅让非终态绑定占用唯一槽位，并以 rebound 终止较旧的活动绑定。
    """

    op.drop_index("device_sessions_tool_uidx", table_name="device_sessions")
    op.add_column(
        "device_sessions",
        sa.Column("tool_session_reference_id", UUID, nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE device_sessions "
            "SET tool_session_reference_id = tool_session_id "
            "WHERE tool_session_reference_id IS NULL"
        )
    )
    op.alter_column("device_sessions", "tool_session_reference_id", nullable=False)
    op.drop_constraint(
        "device_sessions_tool_session_id_fkey",
        "device_sessions",
        type_="foreignkey",
    )
    op.alter_column("device_sessions", "tool_session_id", nullable=True)
    op.create_foreign_key(
        "device_sessions_tool_session_id_fkey",
        "device_sessions",
        "sessions",
        ["tool_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY device_id
                           ORDER BY created_at DESC, id DESC
                       ) AS row_number
                FROM device_sessions
                WHERE status NOT IN ('stopped', 'denied', 'expired', 'failed')
            )
            UPDATE device_sessions
            SET status = 'stopped',
                lease_until = NULL,
                lock_acquired_at = NULL,
                stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP),
                stop_reason = 'rebound'
            WHERE id IN (
                SELECT id FROM ranked WHERE row_number > 1
            )
            """
        )
    )
    op.create_index(
        "device_sessions_tool_uidx",
        "device_sessions",
        ["tool_session_id"],
        unique=True,
        postgresql_where=sa.text(LIVE_STATUS_SQL),
        sqlite_where=sa.text(LIVE_STATUS_SQL),
    )
    op.create_index(
        "device_sessions_device_live_uidx",
        "device_sessions",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text(LIVE_STATUS_SQL),
        sqlite_where=sa.text(LIVE_STATUS_SQL),
    )


def downgrade() -> None:
    """
    恢复全生命周期唯一约束；每个工具会话仅保留最新记录，回退前须导出历史。
    """

    op.drop_index("device_sessions_device_live_uidx", table_name="device_sessions")
    op.drop_index("device_sessions_tool_uidx", table_name="device_sessions")
    op.execute(sa.text("DELETE FROM device_sessions WHERE tool_session_id IS NULL"))
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY tool_session_id
                           ORDER BY created_at DESC, id DESC
                       ) AS row_number
                FROM device_sessions
            )
            DELETE FROM device_sessions
            WHERE id IN (
                SELECT id FROM ranked WHERE row_number > 1
            )
            """
        )
    )
    op.drop_constraint(
        "device_sessions_tool_session_id_fkey",
        "device_sessions",
        type_="foreignkey",
    )
    op.alter_column("device_sessions", "tool_session_id", nullable=False)
    op.create_foreign_key(
        "device_sessions_tool_session_id_fkey",
        "device_sessions",
        "sessions",
        ["tool_session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("device_sessions", "tool_session_reference_id")
    op.create_index(
        "device_sessions_tool_uidx",
        "device_sessions",
        ["tool_session_id"],
        unique=True,
    )
