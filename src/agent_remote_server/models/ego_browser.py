from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON as JsonType
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin, _utc_now

MAX_EGO_BROWSER_GENERATION = 9_223_372_036_854_775_807
MAX_ACTIVE_EGO_BROWSER_GENERATION = MAX_EGO_BROWSER_GENERATION - 1
TERMINAL_EGO_BROWSER_STATUSES = {"stopped", "expired", "failed", "revoked"}
LIVE_EGO_BROWSER_STATUS_SQL = "status NOT IN ('stopped', 'expired', 'failed', 'revoked')"


class EgoBrowserDevice(IdMixin, TimestampMixin, Base):
    """独立的本地 ego-browser 设备身份。"""

    __tablename__ = "ego_browser_devices"
    __table_args__ = (
        CheckConstraint("platform = 'macos'", name="ego_browser_devices_platform_ck"),
        CheckConstraint(
            "status in ('active', 'retiring', 'revoked')",
            name="ego_browser_devices_status_ck",
        ),
        CheckConstraint(
            "release_profile in ('logic-test', 'development-local', "
            "'community-local-trust', 'developer-id')",
            name="ego_browser_devices_release_profile_ck",
        ),
        CheckConstraint(
            "credential_profile in ('community_file', 'keychain_access_group')",
            name="ego_browser_devices_credential_profile_ck",
        ),
        CheckConstraint(
            f"generation between 1 and {MAX_EGO_BROWSER_GENERATION}",
            name="ego_browser_devices_generation_ck",
        ),
        CheckConstraint(
            "allowlist_revision >= 1",
            name="ego_browser_devices_allowlist_revision_ck",
        ),
        Index("ego_browser_devices_user_status_idx", "user_id", "status", "created_at"),
        Index("ego_browser_devices_last_seen_idx", "status", "last_seen_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 旧协议记录可能缺少独立 X25519 公钥；设备重新注册前，这些记录不得激活。
    encryption_public_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="macos")
    release_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    signer_certificate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    bridge_protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    bridge_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_ego_browser_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ego_lite_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    allowlist_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    allowlist_roots_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    learning_bundle_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EgoBrowserDeviceCredential(IdMixin, TimestampMixin, Base):
    """独立 ego-browser 设备客户端的可撤销短期凭据。"""

    __tablename__ = "ego_browser_device_credentials"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'revoked', 'expired')",
            name="ego_browser_device_credentials_status_ck",
        ),
        CheckConstraint(
            "credential_profile in ('community_file', 'keychain_access_group')",
            name="ego_browser_device_credentials_profile_ck",
        ),
        CheckConstraint(
            f"generation between 1 and {MAX_EGO_BROWSER_GENERATION}",
            name="ego_browser_device_credentials_generation_ck",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ego_browser_device_credentials_revision_ck",
        ),
        Index(
            "ego_browser_device_credentials_device_status_idx",
            "ego_browser_device_id",
            "status",
            "expires_at",
        ),
        Index(
            "ego_browser_device_credentials_user_status_idx",
            "user_id",
            "status",
            "expires_at",
        ),
        Index("ego_browser_device_credentials_hash_uidx", "token_hash", unique=True),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ego_browser_device_id: Mapped[UUID] = mapped_column(
        ForeignKey("ego_browser_devices.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    credential_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EgoBrowserBinding(IdMixin, TimestampMixin, Base):
    """一个工具 session 与一个浏览器设备之间的显式全信任绑定。"""

    __tablename__ = "ego_browser_bindings"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending_device', 'connecting', 'probing_local_browser', 'active', "
            "'paused', 'stopping', 'stopped', 'expired', 'failed', 'revoked')",
            name="ego_browser_bindings_status_ck",
        ),
        CheckConstraint(
            "control_channel = 'ego_browser_bridge'",
            name="ego_browser_bindings_control_channel_ck",
        ),
        CheckConstraint(
            "relay_binding_kind = 'ego_browser'",
            name="ego_browser_bindings_relay_kind_ck",
        ),
        CheckConstraint(
            "authorization_mode = 'ego_browser_script_full_trust'",
            name="ego_browser_bindings_authorization_mode_ck",
        ),
        CheckConstraint(
            "authorization_policy_version = 1",
            name="ego_browser_bindings_authorization_policy_ck",
        ),
        CheckConstraint(
            "remote_platform = 'linux' AND local_platform = 'macos'",
            name="ego_browser_bindings_platforms_ck",
        ),
        CheckConstraint(
            "lease_health in ('healthy', 'renewal_grace', 'expired')",
            name="ego_browser_bindings_lease_health_ck",
        ),
        CheckConstraint(
            "concurrency_mode in ('task_space_tab', 'task_space', 'binding')",
            name="ego_browser_bindings_concurrency_mode_ck",
        ),
        CheckConstraint(
            "max_parallel_requests between 1 and 4",
            name="ego_browser_bindings_parallelism_ck",
        ),
        CheckConstraint(
            f"generation between 1 and {MAX_EGO_BROWSER_GENERATION}",
            name="ego_browser_bindings_generation_ck",
        ),
        CheckConstraint(
            f"generation <= {MAX_ACTIVE_EGO_BROWSER_GENERATION} or "
            "status in ('stopped', 'expired', 'failed', 'revoked')",
            name="ego_browser_bindings_active_generation_ck",
        ),
        Index("ego_browser_bindings_user_status_idx", "user_id", "status", "created_at"),
        Index(
            "ego_browser_bindings_device_live_uidx",
            "ego_browser_device_id",
            unique=True,
            sqlite_where=text(LIVE_EGO_BROWSER_STATUS_SQL),
            postgresql_where=text(LIVE_EGO_BROWSER_STATUS_SQL),
        ),
        Index(
            "ego_browser_bindings_tool_live_uidx",
            "tool_session_id",
            unique=True,
            sqlite_where=text(LIVE_EGO_BROWSER_STATUS_SQL),
            postgresql_where=text(LIVE_EGO_BROWSER_STATUS_SQL),
        ),
        Index("ego_browser_bindings_lease_idx", "status", "lease_health", "lease_until"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ego_browser_device_id: Mapped[UUID] = mapped_column(
        ForeignKey("ego_browser_devices.id", ondelete="RESTRICT"), nullable=False
    )
    tool_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    tool_session_reference_id: Mapped[UUID] = mapped_column(nullable=False)
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    control_channel: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ego_browser_bridge"
    )
    relay_binding_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ego_browser"
    )
    authorization_mode: Mapped[str] = mapped_column(
        String(64), nullable=False, default="ego_browser_script_full_trust"
    )
    authorization_policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    release_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    signer_certificate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_platform: Mapped[str] = mapped_column(String(16), nullable=False, default="linux")
    local_platform: Mapped[str] = mapped_column(String(16), nullable=False, default="macos")
    local_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ego_lite_runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bridge_protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    task_space_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    allowlist_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allowlist_roots_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    learning_bundle_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    concurrency_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="binding")
    max_parallel_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    capabilities: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_health: Mapped[str] = mapped_column(String(32), nullable=False, default="healthy")
    lease_grace_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_renew_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    lease_renew_failure_grace_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10
    )
    absolute_ttl_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def binding_tool_session_id(self) -> UUID:
        """
        返回 session 删除后仍保留的稳定引用。

        :return UUID: session 删除后仍稳定保留的工具 session ID
        """

        return self.tool_session_reference_id


class EgoBrowserRequestLedger(IdMixin, Base):
    """记录已认证外层信封的无内容重放状态。"""

    __tablename__ = "ego_browser_request_ledger"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "generation",
            "direction",
            "request_id",
            name="ego_browser_request_ledger_request_uidx",
        ),
        UniqueConstraint(
            "binding_id",
            "generation",
            "direction",
            "sequence",
            name="ego_browser_request_ledger_sequence_uidx",
        ),
        CheckConstraint(
            "direction in ('request', 'response')",
            name="ego_browser_request_ledger_direction_ck",
        ),
        CheckConstraint(
            "message_type in ('execute', 'execute_result', 'cancel')",
            name="ego_browser_request_ledger_type_ck",
        ),
        CheckConstraint(
            "status in ('accepted', 'cancel_requested', 'cancelled', 'completed', 'rejected')",
            name="ego_browser_request_ledger_status_ck",
        ),
        CheckConstraint(
            "payload_bytes >= 0",
            name="ego_browser_request_ledger_payload_bytes_ck",
        ),
        Index(
            "ego_browser_request_ledger_binding_created_idx",
            "binding_id",
            "created_at",
        ),
    )

    binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("ego_browser_bindings.id", ondelete="CASCADE"), nullable=False
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class EgoBrowserRevocationOutbox(IdMixin, Base):
    """等待发布的持久化无内容撤销通知。"""

    __tablename__ = "ego_browser_revocation_outbox"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "generation",
            name="ego_browser_revocation_outbox_binding_generation_uidx",
        ),
        Index("ego_browser_revocation_outbox_pending_idx", "delivered_at", "created_at"),
    )

    binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("ego_browser_bindings.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
