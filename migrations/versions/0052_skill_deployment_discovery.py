"""
新增配置受理时的接管发现边界及独立的首次来源解析，不回填历史计划。
"""

import sqlalchemy as sa
from alembic import op

revision = "0052_skill_deployment_discovery"
down_revision = "0051_skill_deployment_drains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    创建空边界和复合归属来源表，保留旧受理和任务摘要。
    """
    op.create_unique_constraint(
        "skill_takeover_owner_account_uq",
        "skill_account_takeovers",
        ["user_id", "account_id", "id"],
    )
    op.create_table(
        "skill_deployment_discoveries",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("original_digest", sa.String(64), nullable=False),
        sa.Column("directory_epoch", sa.BigInteger(), nullable=False),
        sa.Column("takeover_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_digest", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_targets.user_id",
                "skill_deployment_targets.operation_id",
                "skill_deployment_targets.account_id",
            ],
            name="skill_discovery_target_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "takeover_id"],
            [
                "skill_account_takeovers.user_id",
                "skill_account_takeovers.account_id",
                "skill_account_takeovers.id",
            ],
            name="skill_discovery_takeover_fk",
        ),
        sa.CheckConstraint("directory_epoch >= 1", name="skill_discovery_epoch_ck"),
        sa.CheckConstraint("length(original_digest) = 64", name="skill_discovery_original_ck"),
        sa.CheckConstraint(
            "(takeover_id IS NULL AND resolved_digest IS NULL) OR "
            "(takeover_id IS NOT NULL AND resolved_digest IS NOT NULL "
            "AND length(resolved_digest) = 64)",
            name="skill_discovery_resolution_ck",
        ),
    )
    op.create_table(
        "skill_deployment_discovered_sources",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), primary_key=True),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "operation_id", "account_id"],
            [
                "skill_deployment_discoveries.user_id",
                "skill_deployment_discoveries.operation_id",
                "skill_deployment_discoveries.account_id",
            ],
            name="skill_discovered_target_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "source_id", "revision_id"],
            [
                "account_local_skill_revisions.user_id",
                "account_local_skill_revisions.account_id",
                "account_local_skill_revisions.local_skill_id",
                "account_local_skill_revisions.id",
            ],
            name="skill_discovered_revision_fk",
        ),
        sa.CheckConstraint("length(content_digest) = 64", name="skill_discovered_digest_ck"),
    )


def downgrade() -> None:
    """
    先锁定并拒绝丢弃已有发现边界，不让降级改变原计划的执行语义。
    """
    op.execute(
        sa.text(
            "LOCK TABLE skill_deployment_discoveries, "
            "skill_deployment_discovered_sources IN ACCESS EXCLUSIVE MODE"
        )
    )
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM skill_deployment_discoveries")):
        raise RuntimeError("cannot downgrade recorded skill deployment discoveries")
    op.drop_table("skill_deployment_discovered_sources")
    op.drop_table("skill_deployment_discoveries")
    op.drop_constraint("skill_takeover_owner_account_uq", "skill_account_takeovers", type_="unique")
