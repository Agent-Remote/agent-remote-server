"""
保存先于 Helper 排空的永久原尝试撤权意图，不推断任务已经结束。
"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import TimestampMixin


class SkillDeploymentTermination(TimestampMixin, Base):
    """
    原尝试外键固定全部归属，独立意图身份用于精确排空确认。
    """

    __tablename__ = "skill_deployment_terminations"
    __table_args__ = (
        UniqueConstraint("id", name="skill_deployment_termination_id_uq"),
        CheckConstraint(
            "lease_attempt >= 1 AND lease_attempt <= 2147483647",
            name="skill_deployment_termination_poll_ck",
        ),
        CheckConstraint(
            "outcome IN ('failed', 'superseded')", name="skill_deployment_termination_outcome_ck"
        ),
        CheckConstraint(
            "error_code IN ('NODE_UNAVAILABLE', 'TRANSFER_FAILED', 'QUOTA_EXCEEDED', "
            "'DEPLOYMENT_INTERRUPTED', 'AUTHORIZATION_DENIED', 'SKILL_MANAGER_UNSUPPORTED', "
            "'DEPLOYMENT_INPUT_INVALID', 'OPERATION_SUPERSEDED')",
            name="skill_deployment_termination_error_ck",
        ),
        CheckConstraint(
            "error_code != 'OPERATION_SUPERSEDED' OR outcome = 'superseded'",
            name="skill_deployment_termination_replacement_ck",
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_deployment_tasks.attempt_id"), primary_key=True
    )
    id: Mapped[UUID] = mapped_column(nullable=False)
    lease_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
