"""
保存原始配置部署目标，禁止通过后来的规则重建历史计划。
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_skill_deployment_plans"
down_revision = "0046_skill_sync_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    旧操作保持无计划标记，新增行以复合外键固定用户和来源归属。
    """
    op.add_column("skill_operations", sa.Column("plan_version", sa.Integer(), nullable=True))
    op.create_table(
        "skill_deployment_targets",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("node_id", sa.Uuid(), nullable=True),
        sa.Column("tool_type", sa.String(32), nullable=False),
        sa.Column("runtime_backend", sa.String(32), nullable=True),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id"],
            ["skill_operations.user_id", "skill_operations.id"],
            name="skill_deployment_operation_fk",
        ),
    )
    op.create_table(
        "skill_deployment_entries",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("origin", sa.String(16), primary_key=True),
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=True),
        sa.Column("installation_epoch", sa.BigInteger(), nullable=True),
        sa.Column("package_revision_id", sa.Uuid(), nullable=True),
        sa.Column("local_skill_id", sa.Uuid(), nullable=True),
        sa.Column("local_revision_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_targets.user_id",
                "skill_deployment_targets.operation_id",
                "skill_deployment_targets.account_id",
            ],
            name="skill_deployment_entry_target_fk",
        ),
        sa.CheckConstraint(
            "(origin = 'library' AND installation_id = source_id AND installation_id IS NOT NULL "
            "AND installation_epoch IS NOT NULL AND installation_epoch >= 1 "
            "AND package_revision_id IS NOT NULL AND local_skill_id IS NULL "
            "AND local_revision_id IS NULL) OR "
            "(origin = 'account_local' AND local_skill_id = source_id "
            "AND local_skill_id IS NOT NULL "
            "AND local_revision_id IS NOT NULL AND installation_id IS NULL "
            "AND installation_epoch IS NULL AND package_revision_id IS NULL)",
            name="skill_deployment_entry_source_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "installation_epoch"],
            [
                "skill_installation_epochs.user_id",
                "skill_installation_epochs.installation_id",
                "skill_installation_epochs.epoch",
            ],
            name="skill_deployment_entry_epoch_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "package_revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_deployment_entry_package_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "local_skill_id", "local_revision_id"],
            [
                "account_local_skill_revisions.user_id",
                "account_local_skill_revisions.account_id",
                "account_local_skill_revisions.local_skill_id",
                "account_local_skill_revisions.id",
            ],
            name="skill_deployment_entry_local_fk",
        ),
    )


def downgrade() -> None:
    """
    已记录受理计划时拒绝丢弃无法重建的配置历史。
    """
    op.execute(sa.text("LOCK TABLE skill_operations IN ACCESS EXCLUSIVE MODE"))
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM skill_operations WHERE plan_version IS NOT NULL")
    )
    if count:
        raise RuntimeError("cannot downgrade recorded skill deployment plans")
    op.drop_table("skill_deployment_entries")
    op.drop_table("skill_deployment_targets")
    op.drop_column("skill_operations", "plan_version")
