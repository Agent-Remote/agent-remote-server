"""
为完整树的跨分类删除屏障增加只覆盖不可用对象的查询索引。
"""

import sqlalchemy as sa
from alembic import op

revision = "0055_skill_object_availability"
down_revision = "0054_skill_upload_object_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    仅增加已有状态行的访问路径，不修改内容身份、引用或配额。
    """
    op.create_index(
        "skill_object_unavailable_idx",
        "skill_content_objects",
        ["user_id", "digest"],
        postgresql_where=sa.text("status <> 'available'"),
        sqlite_where=sa.text("status <> 'available'"),
    )


def downgrade() -> None:
    """
    只移除可重建索引，全部原始对象与删除标记继续保留。
    """
    op.drop_index("skill_object_unavailable_idx", table_name="skill_content_objects")
