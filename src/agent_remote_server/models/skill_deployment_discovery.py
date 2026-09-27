"""
保留受理时未知的首次接管边界，独立追加原手工来源而不改写已接受计划。
"""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base


class SkillDeploymentDiscovery(Base):
    """
    受理时固定目标纪元，首次接管提交时一次性封存解析身份和摘要。
    """

    __tablename__ = "skill_deployment_discoveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_targets.user_id",
                "skill_deployment_targets.operation_id",
                "skill_deployment_targets.account_id",
            ],
            name="skill_discovery_target_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "takeover_id"],
            [
                "skill_account_takeovers.user_id",
                "skill_account_takeovers.account_id",
                "skill_account_takeovers.id",
            ],
            name="skill_discovery_takeover_fk",
        ),
        CheckConstraint("directory_epoch >= 1", name="skill_discovery_epoch_ck"),
        CheckConstraint("length(original_digest) = 64", name="skill_discovery_original_ck"),
        CheckConstraint(
            "(takeover_id IS NULL AND resolved_digest IS NULL) OR "
            "(takeover_id IS NOT NULL AND resolved_digest IS NOT NULL "
            "AND length(resolved_digest) = 64)",
            name="skill_discovery_resolution_ck",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    original_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    directory_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    takeover_id: Mapped[UUID | None] = mapped_column(nullable=True)
    resolved_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SkillDeploymentDiscoveredSource(Base):
    """
    补充选择只引用同用户同账户的首次本地版本，不跟随后来的默认值。
    """

    __tablename__ = "skill_deployment_discovered_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_discoveries.user_id",
                "skill_deployment_discoveries.operation_id",
                "skill_deployment_discoveries.account_id",
            ],
            name="skill_discovered_target_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "source_id", "revision_id"],
            [
                "account_local_skill_revisions.user_id",
                "account_local_skill_revisions.account_id",
                "account_local_skill_revisions.local_skill_id",
                "account_local_skill_revisions.id",
            ],
            name="skill_discovered_revision_fk",
        ),
        CheckConstraint("length(content_digest) = 64", name="skill_discovered_digest_ck"),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
