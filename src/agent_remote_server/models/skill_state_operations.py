"""
保存重置恢复的不可变回执及同账户恢复引用。
"""

from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SkillStateOperation(IdMixin, TimestampMixin, Base):
    """
    幂等历史与完整结果同时提交，不能依靠同摘要跨账户恢复。
    """

    __tablename__ = "skill_state_operations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="skill_state_operation_key_uq"),
        CheckConstraint("action IN ('reset', 'restore')", name="skill_state_operation_action_ck"),
        CheckConstraint(
            "scope IN ('item', 'directory') AND result_scope = 'directory'",
            name="skill_state_operation_scope_ck",
        ),
        CheckConstraint(
            "(action = 'reset' AND source_checkpoint_id IS NULL) OR "
            "(action = 'restore' AND source_checkpoint_id IS NOT NULL)",
            name="skill_state_operation_source_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "scope", "source_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_state_operation_source_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "result_scope", "result_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_state_operation_result_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    source_checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    result_checkpoint_id: Mapped[UUID] = mapped_column(nullable=False)
    response_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
