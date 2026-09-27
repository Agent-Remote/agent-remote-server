"""
通过持久化生成列退役历史内容引用，保留全部原始摘要与身份约束。
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_skill_history_retirement"
down_revision = "0040_skill_retention_clocks"
branch_labels = None
depends_on = None

_REFERENCES = {
    "session_skill_snapshots": (("tree_digest", "skill_snapshot_tree_fk"),),
    "skill_finalizations": (("tree_digest", "skill_finalization_tree_fk"),),
    "skill_publications": (("current_tree_digest", "skill_publication_tree_fk"),),
    "skill_branch_preparations": tuple(
        (side + "_digest", "skill_preparation_" + side + "_tree_fk")
        for side in ("base", "current", "incoming")
    ),
    "skill_resolution_choices": (("tree_digest", "skill_resolution_choice_content_fk"),),
    "skill_migration_resolution_content": (
        ("tree_digest", "skill_migration_resolution_content_tree_fk"),
    ),
}
_CHECKS = {
    "session_skill_snapshots": ("skill_snapshot_retired_ck", "status IN ('retained', 'cancelled')"),
    "skill_finalizations": (
        "skill_finalization_retired_ck",
        "status IN ('published', 'conflicted', 'detached')",
    ),
    "skill_publications": ("skill_publication_retired_ck", "status <> 'conflicted'"),
    "skill_branch_preparations": ("skill_preparation_retired_ck", "status <> 'conflicted'"),
}


def _tree_foreign_key(table: str, name: str, column: str) -> None:
    """
    所有内容引用继续绑定精确用户和分类，不能仅凭相同摘要跨用户复用。

    :param table (str): 固定历史表
    :param name (str): 原约束名称
    :param column (str): 审计或有效内容摘要列
    """
    op.create_foreign_key(
        name,
        table,
        "skill_stored_trees",
        ["user_id", "category", column],
        ["user_id", "category", "digest"],
    )


def _checkpoint_foreign_key(column: str) -> None:
    """
    收尾始终绑定同账户完整目录身份，退役后仍保留不可变内容证据。

    :param column (str): checkpoint 原内容身份或旧有效内容列
    """
    name = "skill_finalization_checkpoint_fk"
    op.drop_constraint(name, "skill_finalizations", type_="foreignkey")
    op.create_foreign_key(
        name,
        "skill_finalizations",
        "skill_checkpoints",
        ["user_id", "account_id", "checkpoint_scope", "checkpoint_id", "tree_digest"],
        ["user_id", "account_id", "scope", "id", column],
    )


def upgrade() -> None:
    """
    既有行保持全部内容引用，只有显式退役才使生成外键为空。
    """
    for table, references in _REFERENCES.items():
        op.add_column(
            table, sa.Column("content_retired_at", sa.DateTime(timezone=True), nullable=True)
        )
        for column, name in references:
            retained = "retained_" + column
            op.add_column(
                table,
                sa.Column(
                    retained,
                    sa.String(64),
                    sa.Computed(
                        f"CASE WHEN content_retired_at IS NULL THEN {column} ELSE NULL END",
                        persisted=True,
                    ),
                    nullable=True,
                ),
            )
            op.drop_constraint(name, table, type_="foreignkey")
            _tree_foreign_key(table, name, retained)
        if table in _CHECKS:
            name, condition = _CHECKS[table]
            op.create_check_constraint(
                name, table, "content_retired_at IS NULL OR (" + condition + ")"
            )
    _checkpoint_foreign_key("content_digest")


def downgrade() -> None:
    """
    在任何 schema 变化之前拒绝退役证据，避免无内容时强行恢复旧外键。
    """
    for table in _REFERENCES:
        history = sa.table(table, sa.column("content_retired_at"))
        if (
            op.get_bind()
            .execute(
                sa.select(sa.literal(1))
                .select_from(history)
                .where(history.c.content_retired_at.is_not(None))
                .limit(1)
            )
            .first()
        ):
            raise RuntimeError("retired skill history must be preserved before downgrade")
    _checkpoint_foreign_key("tree_digest")
    for table, references in reversed(tuple(_REFERENCES.items())):
        if table in _CHECKS:
            op.drop_constraint(_CHECKS[table][0], table, type_="check")
        for column, name in references:
            op.drop_constraint(name, table, type_="foreignkey")
            _tree_foreign_key(table, name, column)
            op.drop_column(table, "retained_" + column)
        op.drop_column(table, "content_retired_at")
