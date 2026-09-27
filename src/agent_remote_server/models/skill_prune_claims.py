"""
将已退役内容的续扫资格绑定到真实树或对象行，删除后同摘要重建不继承旧资格。
"""

from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import TimestampMixin


class SkillPruneContentClaim(TimestampMixin, Base):
    """
    派生回收归属随实际内容行删除而消失，既不是硬保护，也不是公开操作回执。
    """

    __tablename__ = "skill_prune_content_claims"
    __table_args__ = (
        UniqueConstraint("id", name="skill_prune_claim_id_uq"),
        CheckConstraint("category = 'state'", name="skill_prune_claim_category_ck"),
        CheckConstraint(
            "(kind = 'tree' AND tree_digest IS NOT NULL "
            "AND tree_digest = digest AND object_digest IS NULL) OR "
            "(kind = 'object' AND object_digest IS NOT NULL "
            "AND object_digest = digest AND tree_digest IS NULL)",
            name="skill_prune_claim_kind_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_prune_claim_account_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            ondelete="CASCADE",
            name="skill_prune_claim_tree_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "category", "object_digest"],
            [
                "skill_content_objects.user_id",
                "skill_content_objects.category",
                "skill_content_objects.digest",
            ],
            ondelete="CASCADE",
            name="skill_prune_claim_object_fk",
        ),
        Index("skill_prune_claim_tree_idx", "user_id", "category", "tree_digest"),
        Index("skill_prune_claim_object_idx", "user_id", "category", "object_digest"),
    )
    id: Mapped[UUID] = mapped_column(nullable=False, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="state")
    tree_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
