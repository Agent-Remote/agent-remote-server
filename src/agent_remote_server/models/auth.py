from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class AuthToken(IdMixin, TimestampMixin, Base):
    """
    可撤销认证令牌
    """

    __tablename__ = "auth_tokens"
    __table_args__ = (
        Index("auth_tokens_hash_uidx", "token_hash", unique=True),
        Index("auth_tokens_user_status_idx", "user_id", "status", "expires_at"),
        Index("auth_tokens_device_status_idx", "user_device_id", "status"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    user_device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_devices.id", ondelete="CASCADE"),
        nullable=True,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CliLoginCode(IdMixin, TimestampMixin, Base):
    """
    CLI device-code 登录记录
    """

    __tablename__ = "cli_login_codes"
    __table_args__ = (
        Index("cli_login_codes_device_hash_uidx", "device_code_hash", unique=True),
        Index("cli_login_codes_user_code_uidx", "user_code", unique=True),
        Index("cli_login_codes_status_idx", "status", "expires_at"),
    )

    device_code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    user_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approved_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_seconds: Mapped[int] = mapped_column(nullable=False, default=5)
