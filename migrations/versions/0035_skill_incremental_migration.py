"""
保存显式增量迁移的精确基线与仅成功递增的分支纪元序号。
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_skill_incremental_migration"
down_revision = "0034_skill_branch_preparation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    既有首次成功迁移只能对应序号一，重复历史使升级失败而不猜测覆盖顺序。
    """
    op.add_column(
        "skill_branch_preparations", sa.Column("base_checkpoint_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "skill_branch_preparations", sa.Column("current_checkpoint_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "skill_branch_preparations", sa.Column("migration_sequence", sa.BigInteger(), nullable=True)
    )
    op.drop_constraint("skill_preparation_mode_ck", "skill_branch_preparations", type_="check")
    op.create_check_constraint(
        "skill_preparation_mode_ck",
        "skill_branch_preparations",
        "mode IN ('initial', 'forward', 'older', 'resume', 'incremental')",
    )
    op.execute(
        sa.text(
            "UPDATE skill_branch_preparations SET migration_sequence = 1 "
            "WHERE mode = 'forward' AND status = 'ready'"
        )
    )
    op.create_unique_constraint(
        "skill_migration_sequence_uq",
        "skill_branch_preparations",
        [
            "source_state_id",
            "target_state_id",
            "source_epoch",
            "target_epoch",
            "directory_epoch",
            "migration_sequence",
        ],
    )
    op.create_check_constraint(
        "skill_migration_sequence_ck",
        "skill_branch_preparations",
        "(mode IN ('forward', 'incremental') AND status = 'ready' "
        "AND migration_sequence IS NOT NULL AND migration_sequence >= 1) OR "
        "((mode NOT IN ('forward', 'incremental') OR status <> 'ready') "
        "AND migration_sequence IS NULL)",
    )
    op.create_check_constraint(
        "skill_migration_distinct_source_ck",
        "skill_branch_preparations",
        "mode NOT IN ('forward', 'incremental') OR "
        "(source_state_id IS NOT NULL AND source_state_id <> target_state_id)",
    )
    for label, state in (("base", "source"), ("current", "target")):
        op.create_foreign_key(
            "skill_migration_" + label + "_checkpoint_fk",
            "skill_branch_preparations",
            "skill_checkpoints",
            ["user_id", "account_id", state + "_state_id", label + "_checkpoint_id"],
            ["user_id", "account_id", "state_id", "id"],
        )


def downgrade() -> None:
    """
    增量历史无法由旧模式无损表达，必须在任何结构修改前拒绝降级。
    """
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM skill_branch_preparations WHERE mode = 'incremental' LIMIT 1")
        )
        .scalar()
    ):
        raise RuntimeError("incremental migration history must be retained before downgrade")
    op.drop_constraint(
        "skill_migration_base_checkpoint_fk", "skill_branch_preparations", type_="foreignkey"
    )
    op.drop_constraint(
        "skill_migration_current_checkpoint_fk", "skill_branch_preparations", type_="foreignkey"
    )
    op.drop_constraint("skill_migration_sequence_uq", "skill_branch_preparations", type_="unique")
    op.drop_constraint("skill_migration_sequence_ck", "skill_branch_preparations", type_="check")
    op.drop_constraint(
        "skill_migration_distinct_source_ck", "skill_branch_preparations", type_="check"
    )
    op.drop_constraint("skill_preparation_mode_ck", "skill_branch_preparations", type_="check")
    op.create_check_constraint(
        "skill_preparation_mode_ck",
        "skill_branch_preparations",
        "mode IN ('initial', 'forward', 'older', 'resume')",
    )
    op.drop_column("skill_branch_preparations", "migration_sequence")
    op.drop_column("skill_branch_preparations", "current_checkpoint_id")
    op.drop_column("skill_branch_preparations", "base_checkpoint_id")
