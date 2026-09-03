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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.device_control_limits import (
    MAX_ACTIVE_DEVICE_SESSION_GENERATION,
    MAX_DEVICE_SESSION_GENERATION,
)
from agent_remote_server.models.mixins import IdMixin, TimestampMixin, _utc_now


class Session(IdMixin, TimestampMixin, Base):
    """
    工具运行会话
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("sessions_project_idx", "user_id", "tool_type", "project_key", "status"),
        Index("sessions_account_active_idx", "tool_account_id", "status"),
    )

    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tool_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    project_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tmux_session_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_backend: Mapped[str] = mapped_column(
        String(32), nullable=False, default="docker_sandbox"
    )
    runtime_resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_control_protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replaces_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )


class SessionEvent(IdMixin, Base):
    """
    工具会话生命周期事件
    """

    __tablename__ = "session_events"
    __table_args__ = (Index("session_events_session_created_idx", "session_id", "created_at"),)

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict[str, object]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class PortForward(IdMixin, TimestampMixin, Base):
    """
    Session 级受控端口转发
    """

    __tablename__ = "port_forwards"
    __table_args__ = (
        CheckConstraint("remote_port between 1 and 65535", name="port_forwards_remote_port_ck"),
        CheckConstraint(
            "requested_local_port between 1 and 65535",
            name="port_forwards_local_port_ck",
        ),
        Index("port_forwards_user_status_idx", "user_id", "status", "created_at"),
        Index("port_forwards_device_status_idx", "device_id", "status", "created_at"),
        Index("port_forwards_session_status_idx", "session_id", "status", "created_at"),
        Index("port_forwards_node_lease_idx", "node_id", "status", "lease_expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_devices.id", ondelete="CASCADE"), nullable=False
    )
    ssh_key_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ssh_keys.id", ondelete="RESTRICT"), nullable=True
    )
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    remote_port: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_local_port: Mapped[int] = mapped_column(Integer, nullable=False)
    client_instance_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    policy_snapshot: Mapped[dict[str, object]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    bytes_up: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_down: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    connection_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    connection_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_bytes_up: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    generation_bytes_down: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    generation_connection_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DeviceSession(IdMixin, TimestampMixin, Base):
    """
    本地设备 GUI 控制会话
    """

    __tablename__ = "device_sessions"
    __table_args__ = (
        CheckConstraint("platform = 'macos'", name="device_sessions_platform_ck"),
        CheckConstraint(
            "authorization_mode in ('per_application_approval', 'session_full_trust')",
            name="device_sessions_authorization_mode_ck",
        ),
        CheckConstraint(
            "authorization_policy_version = 1",
            name="device_sessions_authorization_policy_version_ck",
        ),
        CheckConstraint(
            "(authorization_mode = 'session_full_trust' and authorized_at is not null) or "
            "(authorization_mode = 'per_application_approval' and authorized_at is null)",
            name="device_sessions_full_trust_authorized_at_ck",
        ),
        CheckConstraint(
            f"generation between 1 and {MAX_DEVICE_SESSION_GENERATION}",
            name="device_sessions_generation_ck",
        ),
        CheckConstraint(
            f"generation <= {MAX_ACTIVE_DEVICE_SESSION_GENERATION} or "
            "status in ('stopped', 'denied', 'expired', 'failed')",
            name="device_sessions_active_generation_ck",
        ),
        Index("device_sessions_user_status_idx", "user_id", "status", "created_at"),
        Index("device_sessions_device_status_idx", "device_id", "status", "created_at"),
        Index(
            "device_sessions_tool_uidx",
            "tool_session_id",
            unique=True,
            sqlite_where=text("status NOT IN ('stopped', 'denied', 'expired', 'failed')"),
            postgresql_where=text("status NOT IN ('stopped', 'denied', 'expired', 'failed')"),
        ),
        Index(
            "device_sessions_device_live_uidx",
            "device_id",
            unique=True,
            sqlite_where=text("status NOT IN ('stopped', 'denied', 'expired', 'failed')"),
            postgresql_where=text("status NOT IN ('stopped', 'denied', 'expired', 'failed')"),
        ),
        Index("device_sessions_lease_idx", "status", "lease_until"),
        Index(
            "device_sessions_machine_lock_uidx",
            "device_id",
            unique=True,
            sqlite_where=text("lock_acquired_at IS NOT NULL"),
            postgresql_where=text("lock_acquired_at IS NOT NULL"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_devices.id", ondelete="CASCADE"), nullable=False
    )
    tool_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    tool_session_reference_id: Mapped[UUID] = mapped_column(nullable=False)
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="macos")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    authorization_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="per_application_approval"
    )
    authorization_policy_version: Mapped[int] = mapped_column(nullable=False, default=1)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lock_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @property
    def binding_tool_session_id(self) -> UUID:
        """
        返回历史保留后仍稳定的远端工具 session 引用

        :return UUID: 稳定的远端工具 session 引用

        :raises RuntimeError: 设备会话缺少工具 session 引用时抛出
        """

        value = self.tool_session_reference_id or self.tool_session_id
        if value is None:
            raise RuntimeError("device session is missing its tool-session reference")
        return value


class DeviceSessionApproval(IdMixin, Base):
    """
    设备会话本机应用审批摘要
    """

    __tablename__ = "device_session_approvals"
    __table_args__ = (
        Index(
            "device_session_approvals_session_app_uidx",
            "device_session_id",
            "application_digest",
            unique=True,
        ),
    )

    device_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("device_sessions.id", ondelete="CASCADE"), nullable=False
    )
    application_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    control_level: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_result: Mapped[str] = mapped_column(String(32), nullable=False)
    clipboard_allowed: Mapped[bool] = mapped_column(nullable=False, default=False)
    audit_correlation_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class BrowserSession(IdMixin, TimestampMixin, Base):
    """
    远端临时浏览器会话
    """

    __tablename__ = "browser_sessions"
    __table_args__ = (
        Index("browser_sessions_user_status_idx", "user_id", "status", "created_at"),
        Index("browser_sessions_node_status_idx", "node_id", "status", "expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tool_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tool_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    locale: Mapped[str] = mapped_column(String(64), nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stream_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
