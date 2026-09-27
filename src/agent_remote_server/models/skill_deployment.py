"""
保存原始账户配置计划及受复合外键约束的不可变版本选择。
"""

from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base


class SkillDeploymentTarget(Base):
    """
    历史目标绑定不随账户删除、重绑或节点删除而改写。
    """

    __tablename__ = "skill_deployment_targets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_operations.user_id", "skill_operations.id"],
            name="skill_deployment_operation_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    node_id: Mapped[UUID | None] = mapped_column(nullable=True)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SkillDeploymentEntry(Base):
    """
    两种来源独立约束版本归属，历史摘要不会阻止版本内容退役为墓碑。
    """

    __tablename__ = "skill_deployment_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_targets.user_id",
                "skill_deployment_targets.operation_id",
                "skill_deployment_targets.account_id",
            ],
            name="skill_deployment_entry_target_fk",
        ),
        CheckConstraint(
            "(origin = 'library' AND installation_id = source_id AND installation_id IS NOT NULL "
            "AND installation_epoch IS NOT NULL AND installation_epoch >= 1 "
            "AND package_revision_id IS NOT NULL AND local_skill_id IS NULL "
            "AND local_revision_id IS NULL) OR "
            "(origin = 'account_local' AND local_skill_id = source_id "
            "AND local_skill_id IS NOT NULL "
            "AND local_revision_id IS NOT NULL AND installation_id IS NULL "
            "AND installation_epoch IS NULL AND package_revision_id IS NULL)",
            name="skill_deployment_entry_source_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "installation_epoch"],
            [
                "skill_installation_epochs.user_id",
                "skill_installation_epochs.installation_id",
                "skill_installation_epochs.epoch",
            ],
            name="skill_deployment_entry_epoch_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "package_revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_deployment_entry_package_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "local_skill_id", "local_revision_id"],
            [
                "account_local_skill_revisions.user_id",
                "account_local_skill_revisions.account_id",
                "account_local_skill_revisions.local_skill_id",
                "account_local_skill_revisions.id",
            ],
            name="skill_deployment_entry_local_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    origin: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    installation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    installation_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    package_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    local_skill_id: Mapped[UUID | None] = mapped_column(nullable=True)
    local_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
