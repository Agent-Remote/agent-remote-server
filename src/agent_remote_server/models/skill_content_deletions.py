"""
保留物理删除的原始身份、已释放分类及可重试进度，不把旧任务复用为新内容授权。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SkillContentDeletion(IdMixin, TimestampMixin, Base):
    """
    用户共享摘要的持久化删除屏障，完成记录继续阻止旧任务影响新的同名文件。
    """

    __tablename__ = "skill_content_deletions"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="skill_deletion_owner_uq"),
        CheckConstraint("size >= 0 AND attempts >= 0", name="skill_deletion_counts_ck"),
        CheckConstraint("category_mask IN (1, 2, 3)", name="skill_deletion_categories_ck"),
        CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL) OR "
            "(status = 'complete' AND completed_at IS NOT NULL)",
            name="skill_deletion_status_ck",
        ),
        Index(
            "skill_deletion_pending_uq",
            "user_id",
            "digest",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("skill_deletion_due_idx", "status", "next_attempt_at", "id"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_mask: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
