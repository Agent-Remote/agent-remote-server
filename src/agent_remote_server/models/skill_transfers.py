"""
绑定不可变收尾输入与可续期的当前完整目录上传租约。
"""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import TimestampMixin


class SkillFinalizationTransfer(TimestampMixin, Base):
    """
    租约过期仅推进上传尝试，不允许更换原始输入或混用安装包权限。
    """

    __tablename__ = "skill_finalization_transfers"
    __table_args__ = (
        UniqueConstraint("upload_id", name="skill_transfer_upload_uq"),
        CheckConstraint("attempt >= 1", name="skill_transfer_attempt_ck"),
        CheckConstraint("scope = 'account_directory'", name="skill_transfer_scope_ck"),
        ForeignKeyConstraint(
            ["user_id", "finalization_id", "incoming_digest"],
            [
                "skill_finalizations.user_id",
                "skill_finalizations.id",
                "skill_finalizations.incoming_digest",
            ],
            name="skill_transfer_finalization_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "upload_id", "incoming_digest", "scope"],
            [
                "skill_content_uploads.user_id",
                "skill_content_uploads.id",
                "skill_content_uploads.tree_digest",
                "skill_content_uploads.scope",
            ],
            name="skill_transfer_upload_fk",
        ),
    )
    finalization_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    incoming_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False, default="account_directory")
    upload_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
