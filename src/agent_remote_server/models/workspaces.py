from uuid import UUID

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class Workspace(IdMixin, TimestampMixin, Base):
    """
    用户项目工作区
    """

    __tablename__ = "workspaces"
    __table_args__ = (Index("workspaces_project_idx", "user_id", "project_key"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_devices.id", ondelete="CASCADE"), nullable=False
    )
    project_key: Mapped[str] = mapped_column(String(256), nullable=False)
    local_start_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_git: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    git_sync_policy: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )


class SyncSession(IdMixin, TimestampMixin, Base):
    """
    Mutagen 同步会话
    """

    __tablename__ = "sync_sessions"
    __table_args__ = (Index("sync_sessions_workspace_status_idx", "workspace_id", "status"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
    )
    local_path: Mapped[str] = mapped_column(Text, nullable=False)
    remote_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    conflict_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sync_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    sync_git: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    exclude_patterns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    mutagen_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
