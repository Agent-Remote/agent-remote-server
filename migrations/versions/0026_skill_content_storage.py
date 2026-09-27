"""
增加私有技能内容、完整树和有界上传配额，保持业务引用与文件存储分离。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_skill_content_storage"
down_revision: str | None = "0025_cli_login_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    """
    创建每张表独立的审计时间字段。

    :return list[sa.Column]: 时间字段
    """
    return [
        sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))
        for name in ("created_at", "updated_at")
    ]


def _owner(*, primary_key: bool = True) -> sa.Column:
    """
    创建禁止隐式级联清除私有数据的用户外键。

    :param primary_key (bool): 是否属于联合主键
    :return sa.Column: 用户字段
    """
    return sa.Column(
        "user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=primary_key, nullable=False
    )


def upgrade() -> None:
    """
    登记按用户隔离的内容和上传事务，不自动接管已有账户。
    """
    op.create_table(
        "skill_storage_usage",
        _owner(),
        sa.Column("package_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state_bytes", sa.BigInteger(), nullable=False),
        sa.Column("package_reserved", sa.BigInteger(), nullable=False),
        sa.Column("state_reserved", sa.BigInteger(), nullable=False),
        sa.Column("lock_version", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("package_bytes >= 0 AND state_bytes >= 0", name="skill_usage_bytes_ck"),
        sa.CheckConstraint(
            "package_reserved >= 0 AND state_reserved >= 0", name="skill_usage_reserved_ck"
        ),
    )
    op.create_table(
        "skill_content_objects",
        _owner(),
        sa.Column("category", sa.String(16), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("content_kind", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("category IN ('package', 'state')", name="skill_object_category_ck"),
        sa.CheckConstraint("size >= 0", name="skill_object_size_ck"),
        sa.CheckConstraint("content_kind IN ('text', 'binary')", name="skill_object_kind_ck"),
        sa.CheckConstraint("status IN ('available', 'deleting')", name="skill_object_status_ck"),
    )
    op.create_table(
        "skill_stored_trees",
        _owner(),
        sa.Column("category", sa.String(16), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("category IN ('package', 'state')", name="skill_tree_category_ck"),
        sa.CheckConstraint("total_bytes >= 0", name="skill_tree_bytes_ck"),
    )
    op.create_table(
        "skill_content_uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _owner(primary_key=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("tree_digest", sa.String(64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_upload_request_uq"),
        sa.CheckConstraint(
            "scope IN ('package', 'state', 'account_directory')", name="skill_upload_scope_ck"
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'committed', 'expired')", name="skill_upload_status_ck"
        ),
        sa.CheckConstraint("reserved_bytes >= 0", name="skill_upload_reserved_ck"),
    )
    op.create_index("ix_skill_content_uploads_user_id", "skill_content_uploads", ["user_id"])

    op.create_table(
        "skill_tree_object_references",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("category", sa.String(16), primary_key=True),
        sa.Column("tree_digest", sa.String(64), primary_key=True),
        sa.Column("object_digest", sa.String(64), primary_key=True),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_tree_reference_tree_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "object_digest"],
            [
                "skill_content_objects.user_id",
                "skill_content_objects.category",
                "skill_content_objects.digest",
            ],
            name="skill_tree_reference_object_fk",
        ),
    )


def downgrade() -> None:
    """
    移除内容登记表，磁盘对象仍须由显式导出或清理流程管理。
    """
    op.drop_table("skill_tree_object_references")
    op.drop_table("skill_content_uploads")
    op.drop_table("skill_stored_trees")
    op.drop_table("skill_content_objects")
    op.drop_table("skill_storage_usage")
