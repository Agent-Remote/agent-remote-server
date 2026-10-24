"""
保存工具会话创建调用方原键及不可变受理凭据。
"""

from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SessionCreateRequest(IdMixin, TimestampMixin, Base):
    """
    会话资源删除后仍保留的用户范围创建受理记录。
    """

    __tablename__ = "session_create_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="session_create_requests_user_key_uq"),
        Index("session_create_requests_session_idx", "session_id"),
        CheckConstraint(
            "length(intent_fingerprint) = 71", name="session_create_requests_digest_ck"
        ),
        CheckConstraint("length(argv_digest) = 71", name="session_create_requests_argv_digest_ck"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_account_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    project_key: Mapped[str] = mapped_column(String(256), nullable=False)
    argv_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    intent_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    replacement_session_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    sync_session_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    node_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    session_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    task_record_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    acceptance_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    receipt_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    sync_target_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    observation_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
