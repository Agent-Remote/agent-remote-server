"""
绑定部署尝试、完整目录输入与原节点任务，历史身份不随重试改写。
"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import TimestampMixin


class SkillDeploymentTask(TimestampMixin, Base):
    """
    内容由可退役的目录检查点承载，任务回执本身不是永久内容根。
    """

    __tablename__ = "skill_deployment_tasks"
    __table_args__ = (
        UniqueConstraint("task_id", name="skill_deployment_task_record_uq"),
        ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id", "attempt_id"],
            [
                "skill_deployment_attempts.user_id",
                "skill_deployment_attempts.operation_id",
                "skill_deployment_attempts.account_id",
                "skill_deployment_attempts.id",
            ],
            name="skill_deployment_task_attempt_fk",
        ),
        ForeignKeyConstraint(
            ["node_id", "task_id"],
            ["node_tasks.node_id", "node_tasks.id"],
            name="skill_deployment_task_node_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "checkpoint_id", "content_digest"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
                "skill_checkpoints.content_digest",
            ],
            name="skill_deployment_task_input_fk",
        ),
        CheckConstraint("checkpoint_scope = 'directory'", name="skill_deployment_task_scope_ck"),
        CheckConstraint("length(plan_digest) = 64", name="skill_deployment_task_plan_ck"),
    )
    attempt_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    node_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    checkpoint_id: Mapped[UUID] = mapped_column(nullable=False)
    checkpoint_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
