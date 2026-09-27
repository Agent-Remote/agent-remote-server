"""
保存检查点创建纪元与确切完整目录来源，不猜测既有历史缺失的身份信息。
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_skill_checkpoint_provenance"
down_revision = "0036_skill_migration_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    原数据证据保持未知，新引用通过完整所有权、目录范围与审计摘要绑定。
    """
    op.add_column("skill_checkpoints", sa.Column("state_epoch", sa.BigInteger(), nullable=True))
    op.add_column("skill_checkpoints", sa.Column("directory_epoch", sa.BigInteger(), nullable=True))
    op.add_column("skill_checkpoints", sa.Column("backing_directory_id", sa.Uuid(), nullable=True))
    op.add_column(
        "skill_checkpoints",
        sa.Column(
            "backing_scope", sa.String(length=16), nullable=False, server_default="directory"
        ),
    )
    op.alter_column("skill_checkpoints", "backing_scope", server_default=None)
    op.create_unique_constraint(
        "skill_checkpoint_backing_identity_uq",
        "skill_checkpoints",
        ["user_id", "account_id", "scope", "id", "content_digest"],
    )
    op.create_check_constraint(
        "skill_checkpoint_state_epoch_ck",
        "skill_checkpoints",
        "state_epoch IS NULL OR (scope = 'item' AND state_epoch >= 1)",
    )
    op.create_check_constraint(
        "skill_checkpoint_directory_epoch_ck",
        "skill_checkpoints",
        "directory_epoch IS NULL OR (scope = 'directory' AND directory_epoch >= 1)",
    )
    op.create_check_constraint(
        "skill_checkpoint_backing_scope_ck",
        "skill_checkpoints",
        "backing_scope = 'directory' AND (backing_directory_id IS NULL OR scope = 'item')",
    )
    op.create_foreign_key(
        "skill_checkpoint_backing_fk",
        "skill_checkpoints",
        "skill_checkpoints",
        ["user_id", "account_id", "backing_scope", "backing_directory_id", "content_digest"],
        ["user_id", "account_id", "scope", "id", "content_digest"],
    )


def downgrade() -> None:
    """
    先拒绝删除任何真实来源证据，无证据时完整恢复旧结构而保留历史内容。
    """
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM skill_checkpoints WHERE state_epoch IS NOT NULL "
                "OR directory_epoch IS NOT NULL OR backing_directory_id IS NOT NULL LIMIT 1"
            )
        )
        .scalar()
    ):
        raise RuntimeError("checkpoint provenance must be retained before downgrade")
    op.drop_constraint("skill_checkpoint_backing_fk", "skill_checkpoints", type_="foreignkey")
    for name in ("backing_scope", "directory_epoch", "state_epoch"):
        op.drop_constraint("skill_checkpoint_" + name + "_ck", "skill_checkpoints", type_="check")
    op.drop_constraint("skill_checkpoint_backing_identity_uq", "skill_checkpoints", type_="unique")
    for name in ("backing_scope", "backing_directory_id", "directory_epoch", "state_epoch"):
        op.drop_column("skill_checkpoints", name)
