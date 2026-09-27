"""
保存精确目标的部署尝试链和不重新选取来源的重试受理身份。
"""

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SkillDeploymentAttempt(IdMixin, TimestampMixin, Base):
    """
    终态保留原始身份，后续重试只追加同一目标的直接后继。
    """

    __tablename__ = "skill_deployment_attempts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "operation_id", "account_id", "id", name="skill_attempt_owner_uq"
        ),
        UniqueConstraint(
            "user_id", "operation_id", "account_id", "number", name="skill_attempt_number_uq"
        ),
        UniqueConstraint(
            "user_id",
            "operation_id",
            "account_id",
            "predecessor_id",
            name="skill_attempt_successor_uq",
        ),
        ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_targets.user_id",
                "skill_deployment_targets.operation_id",
                "skill_deployment_targets.account_id",
            ],
            name="skill_attempt_target_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id", "predecessor_id"],
            [
                "skill_deployment_attempts.user_id",
                "skill_deployment_attempts.operation_id",
                "skill_deployment_attempts.account_id",
                "skill_deployment_attempts.id",
            ],
            name="skill_attempt_predecessor_fk",
        ),
        CheckConstraint(
            "number >= 1 AND ((number = 1 AND predecessor_id IS NULL) OR "
            "(number > 1 AND predecessor_id IS NOT NULL AND predecessor_id <> id))",
            name="skill_attempt_sequence_ck",
        ),
        CheckConstraint(
            "status IN ('stored', 'unsupported', 'pending', 'running', 'ready', "
            "'needs_resolution', 'failed', 'superseded')",
            name="skill_attempt_status_ck",
        ),
        CheckConstraint(
            "NOT retryable OR (status = 'failed' AND error_code IS NOT NULL AND "
            "error_code IN ('NODE_UNAVAILABLE', 'TRANSFER_FAILED', 'QUOTA_EXCEEDED', "
            "'DEPLOYMENT_INTERRUPTED'))",
            name="skill_attempt_retry_ck",
        ),
        CheckConstraint("length(plan_digest) = 64", name="skill_attempt_digest_ck"),
        CheckConstraint(
            "(status IN ('stored', 'pending', 'running', 'ready') AND error_code IS NULL) OR "
            "(status IN ('unsupported', 'needs_resolution', 'failed', 'superseded') "
            "AND error_code IS NOT NULL)",
            name="skill_attempt_error_ck",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    predecessor_id: Mapped[UUID | None] = mapped_column(nullable=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SkillDeploymentRetry(IdMixin, TimestampMixin, Base):
    """
    原始重试键与请求摘要阻止响应丢失后重新选择失败项。
    """

    __tablename__ = "skill_deployment_retries"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="skill_deployment_retry_key_uq"),
        ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_operations.user_id", "skill_operations.id"],
            name="skill_deployment_retry_operation_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
