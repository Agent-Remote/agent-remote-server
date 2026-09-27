"""
保存首次目录接管的精确节点任务、不可变输入与单次权威提交收据。
"""

from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class SkillAccountTakeover(IdMixin, TimestampMixin, Base):
    """
    接管记录不随任务结束删除，初始检查点不会被重试替换为后续 head。
    """

    __tablename__ = "skill_account_takeovers"
    __table_args__ = (
        UniqueConstraint("account_id", name="skill_takeover_account_uq"),
        UniqueConstraint("user_id", "account_id", "id", name="skill_takeover_owner_account_uq"),
        UniqueConstraint("user_id", "idempotency_key", name="skill_takeover_key_uq"),
        UniqueConstraint("node_id", "task_id", name="skill_takeover_task_uq"),
        CheckConstraint(
            "status IN ('reserved', 'uploading', 'committed')", name="skill_takeover_status_ck"
        ),
        CheckConstraint(
            "runtime_backend IN ('native', 'docker_sandbox')", name="skill_takeover_backend_ck"
        ),
        CheckConstraint(
            "directory_epoch >= 1 AND upload_attempt >= 0", name="skill_takeover_epoch_ck"
        ),
        CheckConstraint(
            "checkpoint_scope = 'directory' AND upload_scope = 'account_directory'",
            name="skill_takeover_scope_ck",
        ),
        CheckConstraint(
            "(status = 'reserved' AND capture_digest IS NULL AND helper_receipt_id IS NULL "
            "AND upload_id IS NULL AND upload_attempt = 0 AND checkpoint_id IS NULL) OR "
            "(status <> 'reserved' AND capture_digest IS NOT NULL "
            "AND helper_receipt_id IS NOT NULL "
            "AND upload_id IS NOT NULL AND upload_attempt >= 1 AND "
            "((status = 'uploading' AND checkpoint_id IS NULL) OR "
            "(status = 'committed' AND checkpoint_id IS NOT NULL)))",
            name="skill_takeover_phase_ck",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_takeover_directory_fk",
        ),
        ForeignKeyConstraint(
            ["node_id", "task_id"],
            ["node_tasks.node_id", "node_tasks.id"],
            name="skill_takeover_task_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "upload_id", "capture_digest", "upload_scope"],
            [
                "skill_content_uploads.user_id",
                "skill_content_uploads.id",
                "skill_content_uploads.tree_digest",
                "skill_content_uploads.scope",
            ],
            name="skill_takeover_upload_fk",
        ),
        ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "checkpoint_id", "capture_digest"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
                "skill_checkpoints.content_digest",
            ],
            name="skill_takeover_checkpoint_fk",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False)
    account_id: Mapped[UUID] = mapped_column(nullable=False)
    node_id: Mapped[UUID] = mapped_column(nullable=False)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    runtime_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    directory_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    inventory_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    capture_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    helper_receipt_id: Mapped[UUID | None] = mapped_column(nullable=True)
    upload_id: Mapped[UUID | None] = mapped_column(nullable=True)
    upload_attempt: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    upload_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default="account_directory"
    )
    checkpoint_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="directory")
    checkpoint_id: Mapped[UUID | None] = mapped_column(nullable=True)
