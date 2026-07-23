"""add native runtime selection

Revision ID: 0007_native_runtime
Revises: 0006_workspace_git_sync_policy
Create Date: 2026-07-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_native_runtime"
down_revision: str | None = "0006_workspace_git_sync_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column(
            "allowed_runtime_backends",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"docker_sandbox\"]'::jsonb"),
        ),
    )
    op.add_column(
        "nodes",
        sa.Column(
            "default_runtime_backend",
            sa.String(length=32),
            nullable=False,
            server_default="docker_sandbox",
        ),
    )
    op.add_column(
        "nodes",
        sa.Column(
            "runtime_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "nodes",
        sa.Column(
            "runtime_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "tool_accounts",
        sa.Column("runtime_backend", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "runtime_backend",
            sa.String(length=32),
            nullable=False,
            server_default="docker_sandbox",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("runtime_resource_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("replaces_session_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "sessions_replaces_session_id_fkey",
        "sessions",
        "sessions",
        ["replaces_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("sessions_runtime_backend_idx", "sessions", ["runtime_backend", "status"])


def downgrade() -> None:
    op.drop_index("sessions_runtime_backend_idx", table_name="sessions")
    op.drop_constraint("sessions_replaces_session_id_fkey", "sessions", type_="foreignkey")
    op.drop_column("sessions", "replaces_session_id")
    op.drop_column("sessions", "runtime_resource_id")
    op.drop_column("sessions", "runtime_backend")
    op.drop_column("tool_accounts", "runtime_backend")
    op.drop_column("nodes", "runtime_policy")
    op.drop_column("nodes", "runtime_capabilities")
    op.drop_column("nodes", "default_runtime_backend")
    op.drop_column("nodes", "allowed_runtime_backends")
