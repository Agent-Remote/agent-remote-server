from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class WireGuardPeer(IdMixin, TimestampMixin, Base):
    """
    WireGuard peer 记录
    """

    __tablename__ = "wireguard_peers"
    __table_args__ = (
        Index("wireguard_peers_device_idx", "user_device_id"),
        Index("wireguard_peers_node_idx", "node_id"),
    )

    peer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_devices.id", ondelete="CASCADE"),
        nullable=True,
    )
    node_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True
    )
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_private_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
