"""
保存设备登记调用方原键和受保护回执。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class DeviceRegistrationRequest(IdMixin, TimestampMixin, Base):
    """
    用户范围设备登记原键及可恢复的加密令牌回执。
    """

    __tablename__ = "device_registration_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="device_registration_requests_user_key_uq"),
        Index("device_registration_requests_device_idx", "device_id"),
        CheckConstraint(
            "length(intent_fingerprint) = 71", name="device_registration_requests_digest_ck"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    intent_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    device_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    ssh_key_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    wireguard_peer_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    receipt_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    encrypted_device_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
