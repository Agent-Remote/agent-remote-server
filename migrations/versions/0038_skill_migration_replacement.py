"""
保存迁移尝试替代关系与失效原因，旧输入和原始回执不改写。
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_skill_migration_replacement"
down_revision = "0037_skill_checkpoint_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    既有未知关系保持空值，每个旧尝试最多有一个同账户重算子记录。
    """
    table = "skill_branch_preparations"
    for column in ("recomputed_from_id", "replacement_id"):
        op.add_column(table, sa.Column(column, sa.Uuid(), nullable=True))
    op.add_column(table, sa.Column("superseded_reason", sa.String(64), nullable=True))
    op.create_unique_constraint("skill_preparation_recomputed_uq", table, ["recomputed_from_id"])
    for label, condition in (
        ("predecessor", "recomputed_from_id IS NULL OR recomputed_from_id <> id"),
        (
            "replacement",
            "replacement_id IS NULL OR (replacement_id <> id AND status = 'superseded')",
        ),
        ("reason", "superseded_reason IS NULL OR status = 'superseded'"),
    ):
        op.create_check_constraint("skill_preparation_" + label + "_ck", table, condition)
    for column, label in (("recomputed_from_id", "predecessor"), ("replacement_id", "replacement")):
        op.create_foreign_key(
            "skill_preparation_" + label + "_fk",
            table,
            table,
            ["user_id", "account_id", column],
            ["user_id", "account_id", "id"],
        )


def downgrade() -> None:
    """
    任何真实替代或失效证据都阻止降级，空字段可以无损删除。
    """
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM skill_branch_preparations WHERE recomputed_from_id IS NOT NULL "
                "OR replacement_id IS NOT NULL OR superseded_reason IS NOT NULL LIMIT 1"
            )
        )
        .scalar()
    ):
        raise RuntimeError("migration replacement history must be retained before downgrade")
    table = "skill_branch_preparations"
    for label in ("predecessor", "replacement"):
        op.drop_constraint("skill_preparation_" + label + "_fk", table, type_="foreignkey")
    for label in ("predecessor", "replacement", "reason"):
        op.drop_constraint("skill_preparation_" + label + "_ck", table, type_="check")
    op.drop_constraint("skill_preparation_recomputed_uq", table, type_="unique")
    for column in ("recomputed_from_id", "replacement_id", "superseded_reason"):
        op.drop_column(table, column)
