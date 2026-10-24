"""
保存精确 Claude 会话附件传输的短期租约和不可变清理回执。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SessionAttachmentLease(IdMixin, TimestampMixin, Base):
    """
    绑定节点、会话、设备和 SSH 密钥的单用途附件租约。
    """

    __tablename__ = "session_attachment_leases"
    __table_args__ = (
        CheckConstraint(
            "status in ('issued', 'cleanup_pending', 'cleaned', 'expired', 'revoked', 'rejected')",
            name="session_attachment_leases_status_ck",
        ),
        CheckConstraint("max_bytes > 0", name="session_attachment_leases_max_bytes_ck"),
        CheckConstraint(
            "max_entries between 1 and 32", name="session_attachment_leases_max_entries_ck"
        ),
        CheckConstraint(
            "used_bytes >= 0 and used_entries >= 0",
            name="session_attachment_leases_usage_ck",
        ),
        Index("session_attachment_leases_session_idx", "session_id", "status"),
        Index("session_attachment_leases_device_idx", "device_id", "status"),
        Index("session_attachment_leases_expires_idx", "status", "expires_at"),
    )

    session_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    device_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    ssh_key_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    runtime_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="issued")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_entries: Mapped[int] = mapped_column(nullable=False, default=32)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_entries: Mapped[int] = mapped_column(nullable=False, default=0)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SessionAttachmentCleanupReceipt(IdMixin, TimestampMixin, Base):
    """
    保留节点清理结果的公开字段，避免丢失响应后混淆不同租约。
    """

    __tablename__ = "session_attachment_cleanup_receipts"
    __table_args__ = (
        CheckConstraint(
            "status in ('cleaned', 'pending', 'unknown', 'rejected')",
            name="session_attachment_receipts_status_ck",
        ),
        CheckConstraint(
            "removed_batches between 0 and 32",
            name="session_attachment_receipts_batches_ck",
        ),
        Index("session_attachment_receipts_lease_uidx", "lease_id", unique=True),
        Index("session_attachment_receipts_receipt_uidx", "receipt_id", unique=True),
        Index("session_attachment_receipts_session_idx", "session_id", "created_at"),
    )

    lease_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    receipt_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    session_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    node_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    device_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    ssh_key_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    removed_batches: Mapped[int] = mapped_column(nullable=False, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
