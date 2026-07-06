"""create core schema

Revision ID: 0001_core_schema
Revises:
Create Date: 2026-07-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMPTZ = sa.DateTime(timezone=True)


def created_at_column() -> sa.Column:
    """
    创建 created_at 字段

    :return Column: created_at 字段
    """

    return sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()"))


def updated_at_column() -> sa.Column:
    """
    创建 updated_at 字段

    :return Column: updated_at 字段
    """

    return sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    """
    创建核心业务表
    """

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint("role in ('admin', 'user')", name="users_role_ck"),
        sa.CheckConstraint("status in ('active', 'disabled')", name="users_status_ck"),
        sa.UniqueConstraint("username", name="users_username_key"),
    )

    op.create_table(
        "nodes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("region_code", sa.String(length=32), nullable=False),
        sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("wireguard_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "supported_tool_types", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ('healthy', 'degraded', 'maintenance', 'disabled', 'offline')",
            name="nodes_status_ck",
        ),
    )

    op.create_table(
        "user_devices",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint("platform in ('macos', 'linux')", name="user_devices_platform_ck"),
        sa.CheckConstraint("status in ('active', 'revoked')", name="user_devices_status_ck"),
    )

    op.create_table(
        "tool_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("region_code", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("locale", sa.String(length=64), nullable=False),
        sa.Column(
            "preferred_node_tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "affinity_node_id", UUID, sa.ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
        ),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ('pending', 'binding', 'active', 'disabled', 'failed')",
            name="tool_accounts_status_ck",
        ),
    )
    op.create_index("tool_accounts_user_tool_idx", "tool_accounts", ["user_id", "tool_type"])
    op.create_index("tool_accounts_affinity_node_idx", "tool_accounts", ["affinity_node_id"])

    op.create_table(
        "tool_account_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "tool_account_id",
            UUID,
            sa.ForeignKey("tool_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_type", sa.String(length=32), nullable=False),
        sa.Column("profile_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("encrypted_secrets", sa.LargeBinary(), nullable=True),
        created_at_column(),
        updated_at_column(),
    )

    op.create_table(
        "node_heartbeats",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column(
            "supported_tool_types", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("resources", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("runtime", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        created_at_column(),
    )
    op.create_index(
        "node_heartbeats_node_created_idx",
        "node_heartbeats",
        ["node_id", "created_at"],
    )

    op.create_table(
        "node_tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("lease_until", TIMESTAMPTZ, nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ("
            "'pending', 'leased', 'running', 'succeeded', 'failed', 'cancelled', 'expired'"
            ")",
            name="node_tasks_status_ck",
        ),
    )
    op.create_index("node_tasks_task_id_uidx", "node_tasks", ["task_id"], unique=True)
    op.create_index("node_tasks_poll_idx", "node_tasks", ["node_id", "status", "lease_until"])

    op.create_table(
        "node_task_results",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "node_task_id", UUID, sa.ForeignKey("node_tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        sa.Column("started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("finished_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        sa.CheckConstraint("status in ('succeeded', 'failed')", name="node_task_results_status_ck"),
    )
    op.create_index("node_task_results_task_id_idx", "node_task_results", ["task_id"])

    op.create_table(
        "wireguard_peers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("peer_type", sa.String(length=32), nullable=False),
        sa.Column(
            "user_device_id",
            UUID,
            sa.ForeignKey("user_devices.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("encrypted_private_key", sa.LargeBinary(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint("peer_type in ('device', 'node')", name="wireguard_peers_peer_type_ck"),
        sa.CheckConstraint("status in ('active', 'revoked')", name="wireguard_peers_status_ck"),
    )
    op.create_index("wireguard_peers_device_idx", "wireguard_peers", ["user_device_id"])
    op.create_index("wireguard_peers_node_idx", "wireguard_peers", ["node_id"])

    op.create_table(
        "ssh_keys",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "user_device_id",
            UUID,
            sa.ForeignKey("user_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("synced_at", TIMESTAMPTZ, nullable=True),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint("status in ('active', 'revoked')", name="ssh_keys_status_ck"),
    )
    op.create_index("ssh_keys_device_idx", "ssh_keys", ["user_device_id"])

    op.create_table(
        "workspaces",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "device_id", UUID, sa.ForeignKey("user_devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("project_key", sa.String(length=256), nullable=False),
        sa.Column("local_start_path", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("remote_path", sa.Text(), nullable=True),
        created_at_column(),
        updated_at_column(),
    )
    op.create_index("workspaces_project_idx", "workspaces", ["user_id", "project_key"])

    op.create_table(
        "sync_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "workspace_id", UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=False),
        sa.Column("remote_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("conflict_status", sa.String(length=32), nullable=False),
        sa.Column("sync_mode", sa.String(length=32), nullable=False),
        sa.Column("mutagen_session_id", sa.String(length=128), nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ('starting', 'healthy', 'paused', 'conflicted', 'failed', 'stopped')",
            name="sync_sessions_status_ck",
        ),
        sa.CheckConstraint(
            "conflict_status in ('none', 'has_conflicts')",
            name="sync_sessions_conflict_status_ck",
        ),
    )
    op.create_index(
        "sync_sessions_workspace_status_idx", "sync_sessions", ["workspace_id", "status"]
    )

    op.create_table(
        "sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tool_type", sa.String(length=32), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "tool_account_id",
            UUID,
            sa.ForeignKey("tool_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id", UUID, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_key", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tmux_session_name", sa.String(length=128), nullable=True),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ('starting', 'active', 'detached', 'stopping', 'stopped', 'failed')",
            name="sessions_status_ck",
        ),
    )
    op.create_index(
        "sessions_project_idx", "sessions", ["user_id", "tool_type", "project_key", "status"]
    )
    op.create_index("sessions_account_active_idx", "sessions", ["tool_account_id", "status"])

    op.create_table(
        "session_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "session_id", UUID, sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("event_metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        created_at_column(),
    )
    op.create_index(
        "session_events_session_created_idx", "session_events", ["session_id", "created_at"]
    )

    op.create_table(
        "browser_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "tool_account_id",
            UUID,
            sa.ForeignKey("tool_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("region_code", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("locale", sa.String(length=64), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=True),
        sa.Column("container_id", sa.String(length=128), nullable=True),
        sa.Column("stream_endpoint", sa.Text(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("stopped_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.CheckConstraint(
            "status in ('starting', 'ready', 'stopping', 'stopped', 'failed', 'expired')",
            name="browser_sessions_status_ck",
        ),
    )
    op.create_index(
        "browser_sessions_user_status_idx",
        "browser_sessions",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "browser_sessions_node_status_idx",
        "browser_sessions",
        ["node_id", "status", "expires_at"],
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "actor_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        created_at_column(),
    )
    op.create_index("audit_logs_actor_idx", "audit_logs", ["actor_user_id", "created_at"])


def downgrade() -> None:
    """
    删除核心业务表
    """

    op.drop_index("audit_logs_actor_idx", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("browser_sessions_node_status_idx", table_name="browser_sessions")
    op.drop_index("browser_sessions_user_status_idx", table_name="browser_sessions")
    op.drop_table("browser_sessions")
    op.drop_index("session_events_session_created_idx", table_name="session_events")
    op.drop_table("session_events")
    op.drop_index("sessions_account_active_idx", table_name="sessions")
    op.drop_index("sessions_project_idx", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("sync_sessions_workspace_status_idx", table_name="sync_sessions")
    op.drop_table("sync_sessions")
    op.drop_index("workspaces_project_idx", table_name="workspaces")
    op.drop_table("workspaces")
    op.drop_index("ssh_keys_device_idx", table_name="ssh_keys")
    op.drop_table("ssh_keys")
    op.drop_index("wireguard_peers_node_idx", table_name="wireguard_peers")
    op.drop_index("wireguard_peers_device_idx", table_name="wireguard_peers")
    op.drop_table("wireguard_peers")
    op.drop_index("node_task_results_task_id_idx", table_name="node_task_results")
    op.drop_table("node_task_results")
    op.drop_index("node_tasks_poll_idx", table_name="node_tasks")
    op.drop_index("node_tasks_task_id_uidx", table_name="node_tasks")
    op.drop_table("node_tasks")
    op.drop_index("node_heartbeats_node_created_idx", table_name="node_heartbeats")
    op.drop_table("node_heartbeats")
    op.drop_table("tool_account_profiles")
    op.drop_index("tool_accounts_affinity_node_idx", table_name="tool_accounts")
    op.drop_index("tool_accounts_user_tool_idx", table_name="tool_accounts")
    op.drop_table("tool_accounts")
    op.drop_table("user_devices")
    op.drop_table("nodes")
    op.drop_table("users")
