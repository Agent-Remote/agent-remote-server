"""
保存用户冲突解决计划、人工内容引用及独立幂等回执。
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
from agent_remote_server.models.skill_retired_content import (
    SkillRetiredContentMixin,
    retained_digest,
)

_JSON = JSON().with_variant(JSONB(), "postgresql")


class SkillResolutionPlan(TimestampMixin, Base):
    """
    一个发布尝试有一个单调计划版本，取代尝试不会把旧选择复制到新目标。
    """

    __tablename__ = "skill_resolution_plans"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "account_id", "publication_id", name="skill_resolution_plan_owner_uq"
        ),
        CheckConstraint("revision >= 0", name="skill_resolution_plan_revision_ck"),
        ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_publications.user_id",
                "skill_publications.account_id",
                "skill_publications.id",
            ],
            name="skill_resolution_plan_publication_fk",
        ),
    )
    publication_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class SkillResolutionChoice(SkillRetiredContentMixin, TimestampMixin, Base):
    """
    每个互斥范围保留完整侧选择或同用户已验证状态树引用。
    """

    __tablename__ = "skill_resolution_choices"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('current', 'incoming', 'file', 'directory')",
            name="skill_resolution_choice_kind_ck",
        ),
        CheckConstraint("category = 'state'", name="skill_resolution_choice_category_ck"),
        CheckConstraint(
            "(kind IN ('current', 'incoming') AND tree_digest IS NULL) OR "
            "(kind IN ('file', 'directory') AND tree_digest IS NOT NULL)",
            name="skill_resolution_choice_tree_ck",
        ),
        CheckConstraint(
            "kind <> 'file' OR path IS NOT NULL", name="skill_resolution_choice_file_ck"
        ),
        CheckConstraint(
            "kind <> 'directory' OR path IS NULL", name="skill_resolution_choice_directory_ck"
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_resolution_plans.user_id",
                "skill_resolution_plans.account_id",
                "skill_resolution_plans.publication_id",
            ],
            name="skill_resolution_choice_plan_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "retained_tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_resolution_choice_content_fk",
        ),
    )
    publication_id: Mapped[UUID] = mapped_column(primary_key=True)
    selector_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    unit_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)

    retained_tree_digest: Mapped[str | None] = retained_digest("tree_digest")


class SkillResolutionOperation(IdMixin, TimestampMixin, Base):
    """
    重试返回原接受结果，不因后续计划变更重放旧选择。
    """

    __tablename__ = "skill_resolution_operations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="skill_resolution_operation_key_uq"),
        CheckConstraint("plan_revision >= 0", name="skill_resolution_operation_revision_ck"),
        ForeignKeyConstraint(
            ["user_id", "account_id", "publication_id"],
            [
                "skill_publications.user_id",
                "skill_publications.account_id",
                "skill_publications.id",
            ],
            name="skill_resolution_operation_publication_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    publication_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    response_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
