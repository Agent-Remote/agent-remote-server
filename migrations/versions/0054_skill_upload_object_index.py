"""
为原始完整上传清单增加可重建的逐文件声明索引。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0054_skill_upload_object_index"
down_revision = "0053_skill_capture_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    既有上传保持未索引状态，新索引通过原始输入外键约束归属。
    """
    op.add_column(
        "skill_content_uploads",
        sa.Column("object_index_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "skill_content_uploads", sa.Column("object_index_count", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "skill_upload_index_version_ck",
        "skill_content_uploads",
        "(object_index_version = 0 AND object_index_count IS NULL) OR "
        "(object_index_version = 1 AND object_index_count IS NOT NULL AND object_index_count >= 0)",
    )
    op.create_table(
        "skill_upload_objects",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("tree_digest", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("entry_json", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "upload_id", "digest"),
        sa.ForeignKeyConstraint(
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


def downgrade() -> None:
    """
    仅移除可由原始清单重建的投影，不删除上传、计量、引用或磁盘字节。
    """
    op.drop_table("skill_upload_objects")
    op.drop_constraint("skill_upload_index_version_ck", "skill_content_uploads", type_="check")
    op.drop_column("skill_content_uploads", "object_index_count")
    op.drop_column("skill_content_uploads", "object_index_version")
