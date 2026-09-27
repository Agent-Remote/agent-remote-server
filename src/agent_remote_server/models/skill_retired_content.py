"""
分离不可变审计摘要与仍保留内容的生成外键。
"""

from datetime import datetime

from sqlalchemy import Computed, DateTime, String
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column


class SkillRetiredContentMixin:
    """
    退役不改写来源、主键或回执；数据库自动解除有效内容外键。
    """

    content_retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def retained_digest(column: str) -> MappedColumn[str | None]:
    """
    仅用于固定模型定义，生成不可由调用方独立改写的有效摘要列。

    :param column (str): 本表固定的审计摘要列名
    :return MappedColumn[str | None]: 未退役时等于审计摘要的持久化生成列
    """
    if column not in {
        "tree_digest",
        "current_tree_digest",
        "base_digest",
        "current_digest",
        "incoming_digest",
    }:
        raise ValueError("unknown retained digest column")
    return mapped_column(
        String(64),
        Computed(
            f"CASE WHEN content_retired_at IS NULL THEN {column} ELSE NULL END", persisted=True
        ),
        nullable=True,
    )
