"""
保存工具会话停止调用方原键及不可变受理凭据。
"""

from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SessionStopRequest(IdMixin, TimestampMixin, Base):
    """
    会话删除后仍保留的用户范围停止受理记录。
    """

    __tablename__ = "session_stop_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="session_stop_requests_user_key_uq"),
        Index("session_stop_requests_session_idx", "session_id"),
        Index("session_stop_requests_task_idx", "task_record_id"),
        CheckConstraint("length(intent_fingerprint) = 71", name="session_stop_requests_digest_ck"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    session_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    runtime_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_record_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    intent_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    acceptance_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    receipt_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    observation_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
