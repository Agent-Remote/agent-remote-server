"""
保留节点会话的不可变退出分类和可后续补齐的冻结输入，不将停止误记为内容持久化。
"""

from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import TimestampMixin


class SkillSnapshotTermination(TimestampMixin, Base):
    """
    原始归属由不可变快照保留，异常分类不可改变，缺失树摘要只能由完整冻结观察补齐。
    """

    __tablename__ = "skill_snapshot_terminations"
    __table_args__ = (
        CheckConstraint(
            "length(incoming_digest) = 64",
            name="skill_termination_digest_ck",
        ),
        CheckConstraint(
            "(incoming_digest IS NOT NULL AND capture_error IS NULL) OR "
            "(incoming_digest IS NULL AND capture_error IS NOT NULL AND capture_error IN "
            "('quota_exceeded', 'insufficient_storage', 'portability_error', 'capture_failed'))",
            name="skill_termination_capture_ck",
        ),
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("session_skill_snapshots.id"), primary_key=True
    )
    incoming_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capture_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unclean: Mapped[bool] = mapped_column(Boolean, nullable=False)
