"""添加独立 ego-browser Bridge 设备和 binding 记录。

Revision ID: 0018_ego_browser_bridge
Revises: 0017_device_authorization
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_ego_browser_bridge"
down_revision: str | None = "0017_device_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMPTZ = sa.DateTime(timezone=True)
LIVE = "status NOT IN ('stopped', 'expired', 'failed', 'revoked')"


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
    )


def upgrade() -> None:
    """创建独立 ego-browser Bridge 的设备、绑定和生命周期账本。"""

    op.create_table(
        "ego_browser_devices",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("platform", sa.String(length=32), nullable=False, server_default="macos"),
        sa.Column("release_profile", sa.String(length=32), nullable=False),
        sa.Column("signer_certificate_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_profile", sa.String(length=32), nullable=False),
        sa.Column("bridge_protocol_version", sa.String(length=64), nullable=False),
        sa.Column("bridge_version", sa.String(length=64), nullable=True),
        sa.Column("local_ego_browser_runtime_version", sa.String(length=64), nullable=True),
        sa.Column("ego_lite_runtime_version", sa.String(length=64), nullable=True),
        sa.Column("skill_version", sa.String(length=64), nullable=True),
        sa.Column("capabilities", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("allowlist_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("allowlist_roots_digest", sa.String(length=128), nullable=True),
        sa.Column("learning_bundle_digest", sa.String(length=80), nullable=True),
        sa.Column("last_seen_at", TIMESTAMPTZ, nullable=True),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("platform = 'macos'", name="ego_browser_devices_platform_ck"),
        sa.CheckConstraint(
            "status in ('active', 'retiring', 'revoked')",
            name="ego_browser_devices_status_ck",
        ),
        sa.CheckConstraint(
            "release_profile in ("
            "'logic-test', 'development-local', 'community-local-trust', 'developer-id'"
            ")",
            name="ego_browser_devices_release_profile_ck",
        ),
        sa.CheckConstraint(
            "credential_profile in ('community_file', 'keychain_access_group')",
            name="ego_browser_devices_credential_profile_ck",
        ),
        sa.CheckConstraint(
            "generation between 1 and 9223372036854775807",
            name="ego_browser_devices_generation_ck",
        ),
        sa.CheckConstraint(
            "allowlist_revision >= 1",
            name="ego_browser_devices_allowlist_revision_ck",
        ),
    )
    op.create_index(
        "ego_browser_devices_user_status_idx",
        "ego_browser_devices",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ego_browser_devices_last_seen_idx",
        "ego_browser_devices",
        ["status", "last_seen_at"],
    )

    op.create_table(
        "ego_browser_bindings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "ego_browser_device_id",
            UUID,
            sa.ForeignKey("ego_browser_devices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tool_session_id",
            UUID,
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tool_session_reference_id", UUID, nullable=False),
        sa.Column("node_id", UUID, sa.ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_device"),
        sa.Column(
            "control_channel",
            sa.String(length=32),
            nullable=False,
            server_default="ego_browser_bridge",
        ),
        sa.Column(
            "relay_binding_kind", sa.String(length=32), nullable=False, server_default="ego_browser"
        ),
        sa.Column(
            "authorization_mode",
            sa.String(length=64),
            nullable=False,
            server_default="ego_browser_script_full_trust",
        ),
        sa.Column("authorization_policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("authorized_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.Column("release_profile", sa.String(length=32), nullable=False),
        sa.Column("signer_certificate_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_profile", sa.String(length=32), nullable=False),
        sa.Column("remote_platform", sa.String(length=16), nullable=False, server_default="linux"),
        sa.Column("local_platform", sa.String(length=16), nullable=False, server_default="macos"),
        sa.Column("local_runtime_version", sa.String(length=64), nullable=True),
        sa.Column("ego_lite_runtime_version", sa.String(length=64), nullable=True),
        sa.Column("skill_version", sa.String(length=64), nullable=True),
        sa.Column("bridge_protocol_version", sa.String(length=64), nullable=False),
        sa.Column("task_space_label", sa.String(length=256), nullable=True),
        sa.Column("allowlist_revision", sa.BigInteger(), nullable=False),
        sa.Column("allowlist_roots_digest", sa.String(length=128), nullable=True),
        sa.Column("learning_bundle_digest", sa.String(length=80), nullable=True),
        sa.Column(
            "concurrency_mode", sa.String(length=32), nullable=False, server_default="binding"
        ),
        sa.Column("max_parallel_requests", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("capabilities", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("lease_until", TIMESTAMPTZ, nullable=True),
        sa.Column("lease_health", sa.String(length=32), nullable=False, server_default="healthy"),
        sa.Column("lease_grace_until", TIMESTAMPTZ, nullable=True),
        sa.Column(
            "lease_renew_interval_seconds", sa.Integer(), nullable=False, server_default="20"
        ),
        sa.Column(
            "lease_renew_failure_grace_seconds", sa.Integer(), nullable=False, server_default="10"
        ),
        sa.Column("absolute_ttl_until", TIMESTAMPTZ, nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("connected_at", TIMESTAMPTZ, nullable=True),
        sa.Column("stopped_at", TIMESTAMPTZ, nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ("
            "'pending_device', 'connecting', 'probing_local_browser', 'active', 'paused', "
            "'stopping', 'stopped', 'expired', 'failed', 'revoked'"
            ")",
            name="ego_browser_bindings_status_ck",
        ),
        sa.CheckConstraint(
            "control_channel = 'ego_browser_bridge'", name="ego_browser_bindings_control_channel_ck"
        ),
        sa.CheckConstraint(
            "relay_binding_kind = 'ego_browser'", name="ego_browser_bindings_relay_kind_ck"
        ),
        sa.CheckConstraint(
            "authorization_mode = 'ego_browser_script_full_trust'",
            name="ego_browser_bindings_authorization_mode_ck",
        ),
        sa.CheckConstraint(
            "authorization_policy_version = 1", name="ego_browser_bindings_authorization_policy_ck"
        ),
        sa.CheckConstraint(
            "remote_platform = 'linux' AND local_platform = 'macos'",
            name="ego_browser_bindings_platforms_ck",
        ),
        sa.CheckConstraint(
            "lease_health in ('healthy', 'renewal_grace', 'expired')",
            name="ego_browser_bindings_lease_health_ck",
        ),
        sa.CheckConstraint(
            "concurrency_mode in ('task_space_tab', 'task_space', 'binding')",
            name="ego_browser_bindings_concurrency_mode_ck",
        ),
        sa.CheckConstraint(
            "max_parallel_requests between 1 and 4", name="ego_browser_bindings_parallelism_ck"
        ),
        sa.CheckConstraint(
            "generation between 1 and 9223372036854775807",
            name="ego_browser_bindings_generation_ck",
        ),
        sa.CheckConstraint(
            "generation <= 9223372036854775806 or status in ("
            "'stopped', 'expired', 'failed', 'revoked'"
            ")",
            name="ego_browser_bindings_active_generation_ck",
        ),
    )
    op.create_index(
        "ego_browser_bindings_user_status_idx",
        "ego_browser_bindings",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ego_browser_bindings_device_live_uidx",
        "ego_browser_bindings",
        ["ego_browser_device_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
        sqlite_where=sa.text(LIVE),
    )
    op.create_index(
        "ego_browser_bindings_tool_live_uidx",
        "ego_browser_bindings",
        ["tool_session_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
        sqlite_where=sa.text(LIVE),
    )
    op.create_index(
        "ego_browser_bindings_lease_idx",
        "ego_browser_bindings",
        ["status", "lease_health", "lease_until"],
    )

    op.create_table(
        "ego_browser_request_ledger",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "binding_id",
            UUID,
            sa.ForeignKey("ego_browser_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("payload_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="accepted"),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "binding_id",
            "generation",
            "direction",
            "request_id",
            name="ego_browser_request_ledger_request_uidx",
        ),
        sa.UniqueConstraint(
            "binding_id",
            "generation",
            "direction",
            "sequence",
            name="ego_browser_request_ledger_sequence_uidx",
        ),
        sa.CheckConstraint(
            "direction in ('request', 'response')", name="ego_browser_request_ledger_direction_ck"
        ),
        sa.CheckConstraint(
            "message_type in ('execute', 'execute_result')",
            name="ego_browser_request_ledger_type_ck",
        ),
        sa.CheckConstraint(
            "status in ('accepted', 'completed', 'rejected')",
            name="ego_browser_request_ledger_status_ck",
        ),
        sa.CheckConstraint(
            "payload_bytes >= 0", name="ego_browser_request_ledger_payload_bytes_ck"
        ),
    )
    op.create_index(
        "ego_browser_request_ledger_binding_created_idx",
        "ego_browser_request_ledger",
        ["binding_id", "created_at"],
    )

    op.create_table(
        "ego_browser_revocation_outbox",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "binding_id",
            UUID,
            sa.ForeignKey("ego_browser_bindings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("delivered_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "binding_id", "generation", name="ego_browser_revocation_outbox_binding_generation_uidx"
        ),
    )
    op.create_index(
        "ego_browser_revocation_outbox_pending_idx",
        "ego_browser_revocation_outbox",
        ["delivered_at", "created_at"],
    )


def downgrade() -> None:
    """删除独立 ego-browser Bridge 的设备、绑定和生命周期账本。"""

    op.drop_index(
        "ego_browser_revocation_outbox_pending_idx", table_name="ego_browser_revocation_outbox"
    )
    op.drop_table("ego_browser_revocation_outbox")
    op.drop_index(
        "ego_browser_request_ledger_binding_created_idx", table_name="ego_browser_request_ledger"
    )
    op.drop_table("ego_browser_request_ledger")
    op.drop_index("ego_browser_bindings_lease_idx", table_name="ego_browser_bindings")
    op.drop_index("ego_browser_bindings_tool_live_uidx", table_name="ego_browser_bindings")
    op.drop_index("ego_browser_bindings_device_live_uidx", table_name="ego_browser_bindings")
    op.drop_index("ego_browser_bindings_user_status_idx", table_name="ego_browser_bindings")
    op.drop_table("ego_browser_bindings")
    op.drop_index("ego_browser_devices_last_seen_idx", table_name="ego_browser_devices")
    op.drop_index("ego_browser_devices_user_status_idx", table_name="ego_browser_devices")
    op.drop_table("ego_browser_devices")
