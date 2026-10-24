"""
保存工具会话固定集合删除调用方原键及不可变回执。
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


class SessionDeleteRequest(IdMixin, TimestampMixin, Base):
    """
    保存用户审阅时冻结的会话集合删除受理记录。

    删除受理不保存会话外键，避免删除会话时级联移除不可变回执。
    """

    __tablename__ = "session_delete_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="session_delete_requests_user_key_uq"),
        Index("session_delete_requests_selection_idx", "user_id", "selection_digest"),
        CheckConstraint(
            "length(selection_digest) = 71", name="session_delete_requests_selection_ck"
        ),
        CheckConstraint(
            "length(intent_fingerprint) = 71", name="session_delete_requests_intent_ck"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    selection_summary: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    selection_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    intent_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    acceptance_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    receipt_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    observation_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
