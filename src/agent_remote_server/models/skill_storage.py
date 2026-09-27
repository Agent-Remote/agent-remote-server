"""
保存用户私有内容、完整树、上传租约和原子配额预留。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
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


class SkillStorageUsage(TimestampMixin, Base):
    """
    每个用户的存储事务串行点及独立分类配额。
    """

    __tablename__ = "skill_storage_usage"
    __table_args__ = (
        CheckConstraint("package_bytes >= 0 AND state_bytes >= 0", name="skill_usage_bytes_ck"),
        CheckConstraint(
            "package_reserved >= 0 AND state_reserved >= 0", name="skill_usage_reserved_ck"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    package_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    state_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    package_reserved: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    state_reserved: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lock_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class SkillContentObject(TimestampMixin, Base):
    """
    某用户某额度分类内已校验的内容对象，摘要不授予跨用户读取权。
    """

    __tablename__ = "skill_content_objects"
    __table_args__ = (
        CheckConstraint("category IN ('package', 'state')", name="skill_object_category_ck"),
        CheckConstraint("size >= 0", name="skill_object_size_ck"),
        CheckConstraint("content_kind IN ('text', 'binary')", name="skill_object_kind_ck"),
        CheckConstraint("status IN ('available', 'deleting')", name="skill_object_status_ck"),
        Index(
            "skill_object_unavailable_idx",
            "user_id",
            "digest",
            postgresql_where=text("status <> 'available'"),
            sqlite_where=text("status <> 'available'"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    category: Mapped[str] = mapped_column(String(16), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="available", nullable=False)


class SkillStoredTree(SkillHistoryMixin, TimestampMixin, Base):
    """
    仅在全部对象验证成功后登记的完整树。
    """

    __tablename__ = "skill_stored_trees"
    __table_args__ = (
        CheckConstraint("category IN ('package', 'state')", name="skill_tree_category_ck"),
        CheckConstraint("total_bytes >= 0", name="skill_tree_bytes_ck"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    category: Mapped[str] = mapped_column(String(16), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    manifest_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SkillContentUpload(IdMixin, TimestampMixin, Base):
    """
    幂等上传计划及其有界租约，未提交内容不能当作完整树读取。
    """

    __tablename__ = "skill_content_uploads"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="skill_upload_request_uq"),
        UniqueConstraint("user_id", "id", "tree_digest", "scope", name="skill_upload_content_uq"),
        CheckConstraint(
            "scope IN ('package', 'state', 'account_directory')", name="skill_upload_scope_ck"
        ),
        CheckConstraint(
            "status IN ('staged', 'committed', 'expired')", name="skill_upload_status_ck"
        ),
        CheckConstraint("reserved_bytes >= 0", name="skill_upload_reserved_ck"),
        CheckConstraint(
            "(object_index_version = 0 AND object_index_count IS NULL) OR "
            "(object_index_version = 1 AND object_index_count IS NOT NULL "
            "AND object_index_count >= 0)",
            name="skill_upload_index_version_ck",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    tree_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="staged", nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    object_index_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    object_index_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SkillTreeObjectReference(Base):
    """
    完整树对内容对象的显式保活引用，联合外键禁止跨用户关联。
    """

    __tablename__ = "skill_tree_object_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_tree_reference_tree_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "object_digest"],
            [
                "skill_content_objects.user_id",
                "skill_content_objects.category",
                "skill_content_objects.digest",
            ],
            name="skill_tree_reference_object_fk",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(16), primary_key=True)
    tree_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_digest: Mapped[str] = mapped_column(String(64), primary_key=True)


class SkillUploadObject(Base):
    """
    原始上传清单的逐对象声明索引，不代表字节已经接收或内容已经提交。
    """

    __tablename__ = "skill_upload_objects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "upload_id", "tree_digest", "scope"],
            [
                "skill_content_uploads.user_id",
                "skill_content_uploads.id",
                "skill_content_uploads.tree_digest",
                "skill_content_uploads.scope",
            ],
            name="skill_upload_object_input_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    upload_id: Mapped[UUID] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    tree_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    entry_json: Mapped[dict[str, object]] = mapped_column(_JSON, nullable=False)
