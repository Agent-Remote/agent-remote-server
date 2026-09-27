"""
保存用户技能库、不可变版本、安装纪元和逐字段覆盖规则。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin
from agent_remote_server.models.skill_history import SkillHistoryMixin

_JSON = JSON().with_variant(JSONB(), "postgresql")


class SkillLibrary(TimestampMixin, Base):
    """
    用户库配置代数，与账户运行 head 和 epoch 独立。
    """

    __tablename__ = "skill_libraries"
    __table_args__ = (CheckConstraint("generation >= 0", name="skill_library_generation_ck"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class SkillInstallation(IdMixin, TimestampMixin, Base):
    """
    同一用户、来源与子路径的稳定身份，卸载只归档当前纪元。
    """

    __tablename__ = "skill_installations"
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="skill_installation_owner_uq"),
        UniqueConstraint("user_id", "source_key", name="skill_installation_source_uq"),
        CheckConstraint("epoch >= 1", name="skill_installation_epoch_ck"),
        Index(
            "skill_installation_active_name_uq",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("removed = false"),
            sqlite_where=text("removed = 0"),
        ),
        ForeignKeyConstraint(
            ["user_id", "id", "default_revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_installation_default_revision_fk",
            use_alter=True,
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("skill_libraries.user_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    tracking_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    default_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    default_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    removed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SkillInstallationEpoch(TimestampMixin, Base):
    """
    保留卸载前后的独立归属，供旧会话收尾和显式恢复引用。
    """

    __tablename__ = "skill_installation_epochs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_epoch_installation_fk",
        ),
        CheckConstraint("epoch >= 1", name="skill_epoch_number_ck"),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SkillRevision(SkillHistoryMixin, IdMixin, TimestampMixin, Base):
    """
    不可变内容版本，来源观测另存以免重复获取改写历史。
    """

    __tablename__ = "skill_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "installation_id", "id", name="skill_revision_owner_uq"),
        UniqueConstraint("installation_id", "number", name="skill_revision_number_uq"),
        UniqueConstraint("installation_id", "content_digest", name="skill_revision_content_uq"),
        CheckConstraint("number >= 1", name="skill_revision_number_ck"),
        CheckConstraint("category = 'package'", name="skill_revision_category_ck"),
        ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_revision_installation_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_revision_tree_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    installation_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(String(16), default="package", nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    retained: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SkillToolOverride(TimestampMixin, Base):
    """
    工具范围逐字段覆盖，空字段始终表示继承。
    """

    __tablename__ = "skill_tool_overrides"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_tool_installation_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_tool_revision_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(primary_key=True)
    tool_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    revision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class SkillAccountOverride(TimestampMixin, Base):
    """
    账户范围逐字段覆盖，复合外键固定用户、工具与版本归属。
    """

    __tablename__ = "skill_account_overrides"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "installation_id"],
            ["skill_installations.user_id", "skill_installations.id"],
            name="skill_account_installation_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "tool_type"],
            ["tool_accounts.user_id", "tool_accounts.id", "tool_accounts.tool_type"],
            name="skill_override_account_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_account_revision_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    installation_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    revision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class SkillActivation(IdMixin, TimestampMixin, Base):
    """
    用户默认版本实际激活历史，候选版本不写入此表。
    """

    __tablename__ = "skill_activations"
    __table_args__ = (
        UniqueConstraint("installation_id", "generation", name="skill_activation_generation_uq"),
        ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_activation_revision_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    installation_id: Mapped[UUID] = mapped_column(nullable=False)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SkillSourceObservation(IdMixin, TimestampMixin, Base):
    """
    每次获取上游的独立来源审计，不改写相同内容既有版本。
    """

    __tablename__ = "skill_source_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "installation_id", "revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_observation_revision_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    installation_id: Mapped[UUID] = mapped_column(nullable=False)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    provenance_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)


class SkillOperation(IdMixin, TimestampMixin, Base):
    """
    与配置变更同时提交的幂等受理结果和部署状态入口。
    """

    __tablename__ = "skill_operations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="skill_operation_key_uq"),
        UniqueConstraint("user_id", "id", name="skill_operation_owner_uq"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    result_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    committed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    replacement_id: Mapped[UUID | None] = mapped_column(nullable=True)
    plan_version: Mapped[int | None] = mapped_column(nullable=True)
    attempts_version: Mapped[int | None] = mapped_column(nullable=True)
