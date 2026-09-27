"""
增加与不可变收尾输入绑定、允许显式续期的上传尝试。
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_skill_finalization_uploads"
down_revision = "0028_skill_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    先固定父记录的归属与内容唯一键，再创建不级联删除的传输引用。
    """
    op.create_unique_constraint(
        "skill_finalization_input_uq", "skill_finalizations", ["user_id", "id", "incoming_digest"]
    )
    op.create_unique_constraint(
        "skill_upload_content_uq",
        "skill_content_uploads",
        ["user_id", "id", "tree_digest", "scope"],
    )
    op.create_table(
        "skill_finalization_transfers",
        sa.Column("finalization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("incoming_digest", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("finalization_id"),
        sa.UniqueConstraint("upload_id", name="skill_transfer_upload_uq"),
        sa.CheckConstraint("attempt >= 1", name="skill_transfer_attempt_ck"),
        sa.CheckConstraint("scope = 'account_directory'", name="skill_transfer_scope_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "finalization_id", "incoming_digest"],
            [
                "skill_finalizations.user_id",
                "skill_finalizations.id",
                "skill_finalizations.incoming_digest",
            ],
            name="skill_transfer_finalization_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "upload_id", "incoming_digest", "scope"],
            [
                "skill_content_uploads.user_id",
                "skill_content_uploads.id",
                "skill_content_uploads.tree_digest",
                "skill_content_uploads.scope",
            ],
            name="skill_transfer_upload_fk",
        ),
    )


def downgrade() -> None:
    """
    先解除传输引用，再恢复父表的原始约束集合。
    """
    op.drop_table("skill_finalization_transfers")
    op.drop_constraint("skill_upload_content_uq", "skill_content_uploads", type_="unique")
    op.drop_constraint("skill_finalization_input_uq", "skill_finalizations", type_="unique")
