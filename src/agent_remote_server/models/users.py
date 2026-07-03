from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    """
    管理端用户
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserDevice(IdMixin, TimestampMixin, Base):
    """
    用户本地设备
    """

    __tablename__ = "user_devices"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SshKey(IdMixin, TimestampMixin, Base):
    """
    设备级 SSH 公钥
    """

    __tablename__ = "ssh_keys"
    __table_args__ = (Index("ssh_keys_device_idx", "user_device_id"),)

    user_device_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
