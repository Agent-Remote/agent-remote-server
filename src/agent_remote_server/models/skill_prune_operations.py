"""
永久保存 prune 原受理、完整披露和删除任务关联，不保活已退役内容。
"""

from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SkillPruneOperation(IdMixin, TimestampMixin, Base):
    """
    原键和精确请求摘要与不可变受理共享事务，不保存原始确认凭据。
    """

    __tablename__ = "skill_prune_operations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="skill_prune_operation_key_uq"),
        UniqueConstraint("user_id", "id", name="skill_prune_operation_owner_uq"),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_prune_operation_account_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )


class SkillPruneOperationEntry(Base):
    """
    保存全部原候选与阻断及实际替换身份，旧内容消失后仍可完整恢复披露。
    """

    __tablename__ = "skill_prune_operation_entries"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="skill_prune_entry_ordinal_ck"),
        ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_prune_operations.user_id", "skill_prune_operations.id"],
            name="skill_prune_entry_operation_fk",
        ),
    )
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    disclosure_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )


class SkillPruneOperationDeletion(Base):
    """
    只链接同所有者的原始实际任务，不让新的同摘要上传继承历史删除结果。
    """

    __tablename__ = "skill_prune_operation_deletions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_prune_operations.user_id", "skill_prune_operations.id"],
            name="skill_prune_deletion_operation_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "deletion_id"],
            ["skill_content_deletions.user_id", "skill_content_deletions.id"],
            name="skill_prune_deletion_task_fk",
        ),
    )
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    deletion_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
