"""
保存迁移专用人工内容授权、版本化计划和不可变操作回执。
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


class SkillMigrationResolutionUpload(TimestampMixin, Base):
    """
    上传租约以真实复合外键绑定迁移，不能只依赖可猜测的幂等键前缀。
    """

    __tablename__ = "skill_migration_resolution_uploads"
    __table_args__ = (
        CheckConstraint(
            "scope = 'account_directory'", name="skill_migration_resolution_upload_scope_ck"
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            ["skill_branch_preparations." + name for name in ("user_id", "account_id", "id")],
            name="skill_migration_resolution_upload_migration_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "upload_id", "tree_digest", "scope"],
            ["skill_content_uploads." + name for name in ("user_id", "id", "tree_digest", "scope")],
            name="skill_migration_resolution_upload_content_fk",
        ),
    )
    upload_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    migration_id: Mapped[UUID] = mapped_column(nullable=False)
    tree_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False, default="account_directory")


class SkillMigrationResolutionContent(SkillRetiredContentMixin, TimestampMixin, Base):
    """
    完成验证的人工树仅授权给原迁移，其他同用户同摘要内容不能冒用。
    """

    __tablename__ = "skill_migration_resolution_content"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_id",
            "migration_id",
            "category",
            "tree_digest",
            name="skill_migration_resolution_content_owner_uq",
        ),
        CheckConstraint(
            "category = 'state'", name="skill_migration_resolution_content_category_ck"
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            ["skill_branch_preparations." + name for name in ("user_id", "account_id", "id")],
            name="skill_migration_resolution_content_migration_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "retained_tree_digest"],
            ["skill_stored_trees." + name for name in ("user_id", "category", "digest")],
            name="skill_migration_resolution_content_tree_fk",
        ),
    )
    migration_id: Mapped[UUID] = mapped_column(primary_key=True)
    tree_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")

    retained_tree_digest: Mapped[str | None] = retained_digest("tree_digest")


class SkillMigrationResolutionPlan(TimestampMixin, Base):
    """
    一个迁移冲突有一个单调计划版本，原始响应不受后续选择影响。
    """

    __tablename__ = "skill_migration_resolution_plans"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "account_id", "migration_id", name="skill_migration_resolution_plan_owner_uq"
        ),
        CheckConstraint("revision >= 0", name="skill_migration_resolution_plan_revision_ck"),
        ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            [
                "skill_branch_preparations.user_id",
                "skill_branch_preparations.account_id",
                "skill_branch_preparations.id",
            ],
            name="skill_migration_resolution_plan_migration_fk",
        ),
    )
    migration_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class SkillMigrationResolutionChoice(TimestampMixin, Base):
    """
    每个互斥范围只允许引用本迁移明确授权的人工状态树。
    """

    __tablename__ = "skill_migration_resolution_choices"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('current', 'incoming', 'file', 'directory')",
            name="skill_migration_resolution_choice_kind_ck",
        ),
        CheckConstraint("category = 'state'", name="skill_migration_resolution_choice_category_ck"),
        CheckConstraint(
            "(kind IN ('current', 'incoming') AND tree_digest IS NULL) OR "
            "(kind IN ('file', 'directory') AND tree_digest IS NOT NULL)",
            name="skill_migration_resolution_choice_tree_ck",
        ),
        CheckConstraint(
            "kind <> 'file' OR path IS NOT NULL", name="skill_migration_resolution_choice_file_ck"
        ),
        CheckConstraint(
            "kind <> 'directory' OR path IS NULL",
            name="skill_migration_resolution_choice_directory_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            [
                "skill_migration_resolution_plans.user_id",
                "skill_migration_resolution_plans.account_id",
                "skill_migration_resolution_plans.migration_id",
            ],
            name="skill_migration_resolution_choice_plan_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id", "category", "tree_digest"],
            [
                "skill_migration_resolution_content." + column
                for column in ("user_id", "account_id", "migration_id", "category", "tree_digest")
            ],
            name="skill_migration_resolution_choice_content_fk",
        ),
    )
    migration_id: Mapped[UUID] = mapped_column(primary_key=True)
    selector_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    unit_json: Mapped[list[str]] = mapped_column(_JSON, nullable=False, default=list)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SkillMigrationResolutionOperation(IdMixin, TimestampMixin, Base):
    """
    重试返回原接受结果，不因后续计划变更重放旧选择。
    """

    __tablename__ = "skill_migration_resolution_operations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="skill_migration_resolution_operation_key_uq"
        ),
        CheckConstraint(
            "plan_revision >= 0", name="skill_migration_resolution_operation_revision_ck"
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "migration_id"],
            [
                "skill_branch_preparations.user_id",
                "skill_branch_preparations.account_id",
                "skill_branch_preparations.id",
            ],
            name="skill_migration_resolution_operation_migration_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    migration_id: Mapped[UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    response_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
