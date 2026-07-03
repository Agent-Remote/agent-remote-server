from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON as JsonType
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
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
