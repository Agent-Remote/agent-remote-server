"""
记录历史最后解除有效保护的时间，不以创建或扫描时间补全旧证据。
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


class SkillHistoryMixin:
    """
    空值表示仍受保护或未知释放时间，不能直接推断已到期。
    """

    retention_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
