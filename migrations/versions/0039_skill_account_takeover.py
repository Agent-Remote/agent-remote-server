"""
固定首次账户目录接管任务、旧写入者清单和原子权威提交收据。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039_skill_account_takeover"
down_revision = "0038_skill_migration_replacement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    新增归属及精确内容复合约束，不改变任何已有账户目录模式。
    """
    op.create_table(
        "skill_account_takeovers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_backend", sa.String(32), nullable=False),
        sa.Column("directory_epoch", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("inventory_digest", sa.String(64), nullable=False),
        sa.Column(
            "inventory_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("capture_digest", sa.String(64), nullable=True),
        sa.Column("helper_receipt_id", sa.Uuid(), nullable=True),
        sa.Column("upload_id", sa.Uuid(), nullable=True),
        sa.Column("upload_attempt", sa.BigInteger(), nullable=False),
        sa.Column("upload_scope", sa.String(24), nullable=False),
        sa.Column("checkpoint_scope", sa.String(16), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", name="skill_takeover_account_uq"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_takeover_key_uq"),
        sa.UniqueConstraint("node_id", "task_id", name="skill_takeover_task_uq"),
        sa.CheckConstraint(
            "status IN ('reserved', 'uploading', 'committed')", name="skill_takeover_status_ck"
        ),
        sa.CheckConstraint(
            "runtime_backend IN ('native', 'docker_sandbox')", name="skill_takeover_backend_ck"
        ),
        sa.CheckConstraint(
            "directory_epoch >= 1 AND upload_attempt >= 0", name="skill_takeover_epoch_ck"
        ),
        sa.CheckConstraint(
            "checkpoint_scope = 'directory' AND upload_scope = 'account_directory'",
            name="skill_takeover_scope_ck",
        ),
        sa.CheckConstraint(
            "(status = 'reserved' AND capture_digest IS NULL AND helper_receipt_id IS "
            "NULL AND upload_id IS NULL AND upload_attempt = 0 AND checkpoint_id IS "
            "NULL) OR (status <> 'reserved' AND capture_digest IS NOT NULL AND "
            "helper_receipt_id IS NOT NULL AND upload_id IS NOT NULL AND upload_attempt "
            ">= 1 AND ((status = 'uploading' AND checkpoint_id IS NULL) OR (status = "
            "'committed' AND checkpoint_id IS NOT NULL)))",
            name="skill_takeover_phase_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_takeover_directory_fk",
        ),
        sa.ForeignKeyConstraint(
            ["node_id", "task_id"],
            ["node_tasks.node_id", "node_tasks.id"],
            name="skill_takeover_task_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "upload_id", "capture_digest", "upload_scope"],
            [
                "skill_content_uploads.user_id",
                "skill_content_uploads.id",
                "skill_content_uploads.tree_digest",
                "skill_content_uploads.scope",
            ],
            name="skill_takeover_upload_fk",
        ),
        sa.ForeignKeyConstraint(
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


def downgrade() -> None:
    """
    任何接管或未完成上传都阻止降级，不能丢弃旧目录权威切换证据。
    """
    if op.get_bind().execute(sa.text("SELECT 1 FROM skill_account_takeovers LIMIT 1")).scalar():
        raise RuntimeError("account takeover history must be retained before downgrade")
    op.drop_table("skill_account_takeovers")
