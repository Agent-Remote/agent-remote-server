"""
保存配置导入的原始调用方身份及无内容受理凭据。
"""

from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class ToolAccountConfigImportRequest(IdMixin, TimestampMixin, Base):
    """
    历史账户和任务身份不随资源删除释放原调用方键。
    """

    __tablename__ = "tool_account_config_import_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="config_import_requests_user_key_uq"),
        UniqueConstraint("task_record_id", name="config_import_requests_task_uq"),
        CheckConstraint("length(intent_fingerprint) = 71", name="config_import_requests_digest_ck"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    tool_account_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_record_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[str] = mapped_column(String(256), nullable=False)
    intent_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    receipt_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
