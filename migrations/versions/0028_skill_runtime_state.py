"""
增加账户运行分支、目录 checkpoint、精确会话快照和收尾引用。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_skill_runtime_state"
down_revision: str | None = "0027_skill_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    建立同用户账户、节点任务和完整目录内容的强引用边界。
    """
    op.create_unique_constraint("node_tasks_owner_uq", "node_tasks", ["node_id", "id"])
    op.create_unique_constraint(
        "sessions_skill_binding_uq", "sessions", ["user_id", "tool_account_id", "node_id", "id"]
    )
    op.create_table(
        "account_skill_directory_states",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tool_type", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("head_scope", sa.String(length=16), nullable=False),
        sa.Column("head_checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("head_scope = 'directory'", name="skill_directory_scope_ck"),
        sa.CheckConstraint(
            "mode <> 'managed_v1' OR head_checkpoint_id IS NOT NULL",
            name="skill_directory_managed_head_ck",
        ),
        sa.CheckConstraint(
            "mode IN ('legacy', 'migrating', 'managed_v1')", name="skill_directory_mode_ck"
        ),
        sa.CheckConstraint("epoch >= 1", name="skill_directory_epoch_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "tool_type"],
            ["tool_accounts.user_id", "tool_accounts.id", "tool_accounts.tool_type"],
            name="skill_directory_account_fk",
        ),
        sa.PrimaryKeyConstraint("account_id"),
        sa.UniqueConstraint("user_id", "account_id", name="skill_directory_owner_uq"),
    )
    op.create_table(
        "account_skill_states",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column("installation_epoch", sa.BigInteger(), nullable=False),
        sa.Column("base_revision_id", sa.Uuid(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("head_checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("expired", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("epoch >= 1", name="skill_state_epoch_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_state_directory_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "base_revision_id"],
            ["skill_revisions.user_id", "skill_revisions.installation_id", "skill_revisions.id"],
            name="skill_state_revision_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "installation_id", "installation_epoch"],
            [
                "skill_installation_epochs.user_id",
                "skill_installation_epochs.installation_id",
                "skill_installation_epochs.epoch",
            ],
            name="skill_state_installation_epoch_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "installation_id",
            "installation_epoch",
            "base_revision_id",
            name="skill_state_branch_uq",
        ),
        sa.UniqueConstraint("user_id", "account_id", "id", name="skill_state_owner_uq"),
    )
    op.create_table(
        "skill_checkpoints",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("state_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=True),
        sa.Column("subtree_prefix", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("source_session_reference_id", sa.Uuid(), nullable=True),
        sa.Column("retained", sa.Boolean(), nullable=False),
        sa.Column("invalid_skill_format", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'directory' AND state_id IS NULL AND subtree_prefix = '') OR "
            "(scope = 'item' AND state_id IS NOT NULL AND subtree_prefix <> '')",
            name="skill_checkpoint_view_ck",
        ),
        sa.CheckConstraint("category = 'state'", name="skill_checkpoint_category_ck"),
        sa.CheckConstraint(
            "(retained = true AND tree_digest IS NOT NULL AND tree_digest = "
            "content_digest) OR (retained = false AND tree_digest IS NULL)",
            name="skill_checkpoint_retention_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "parent_id"],
            ["skill_checkpoints.user_id", "skill_checkpoints.account_id", "skill_checkpoints.id"],
            name="skill_checkpoint_parent_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "state_id"],
            [
                "account_skill_states.user_id",
                "account_skill_states.account_id",
                "account_skill_states.id",
            ],
            name="skill_checkpoint_state_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_checkpoint_directory_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_checkpoint_tree_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "account_id", "id", name="skill_checkpoint_owner_uq"),
        sa.UniqueConstraint(
            "user_id",
            "account_id",
            "scope",
            "id",
            "tree_digest",
            name="skill_checkpoint_content_uq",
        ),
        sa.UniqueConstraint(
            "user_id", "account_id", "scope", "id", name="skill_checkpoint_scope_uq"
        ),
        sa.UniqueConstraint(
            "user_id", "account_id", "state_id", "id", name="skill_checkpoint_branch_uq"
        ),
    )
    op.create_table(
        "session_skill_snapshots",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("session_reference_id", sa.Uuid(), nullable=False),
        sa.Column("prepare_task_id", sa.Uuid(), nullable=True),
        sa.Column("runtime_backend", sa.String(length=32), nullable=False),
        sa.Column("library_generation", sa.BigInteger(), nullable=False),
        sa.Column("directory_epoch", sa.BigInteger(), nullable=False),
        sa.Column("starting_scope", sa.String(length=16), nullable=False),
        sa.Column("starting_checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "system_releases_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category = 'state' AND starting_scope = 'directory'", name="skill_snapshot_scope_ck"
        ),
        sa.CheckConstraint(
            "runtime_backend IN ('native', 'docker_sandbox')", name="skill_snapshot_backend_ck"
        ),
        sa.CheckConstraint(
            "session_id IS NOT NULL OR status IN ('retained', 'cancelled')",
            name="skill_snapshot_live_session_ck",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'started', 'finalizing', 'retained', 'cancelled')",
            name="skill_snapshot_status_ck",
        ),
        sa.CheckConstraint(
            "library_generation >= 0 AND directory_epoch >= 1", name="skill_snapshot_generation_ck"
        ),
        sa.CheckConstraint(
            "session_id IS NULL OR session_id = session_reference_id",
            name="skill_snapshot_session_ck",
        ),
        sa.ForeignKeyConstraint(
            ["node_id", "prepare_task_id"],
            ["node_tasks.node_id", "node_tasks.id"],
            name="skill_snapshot_task_fk",
        ),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"]),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "node_id", "session_id"],
            ["sessions.user_id", "sessions.tool_account_id", "sessions.node_id", "sessions.id"],
            name="skill_snapshot_session_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "starting_scope", "starting_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_snapshot_starting_head_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id"],
            ["account_skill_directory_states.user_id", "account_skill_directory_states.account_id"],
            name="skill_snapshot_directory_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_snapshot_tree_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_reference_id", name="skill_snapshot_session_uq"),
        sa.UniqueConstraint("user_id", "account_id", "id", name="skill_snapshot_owner_uq"),
        sa.UniqueConstraint(
            "user_id", "account_id", "node_id", "id", name="skill_snapshot_node_uq"
        ),
    )
    op.create_table(
        "skill_directory_members",
        sa.Column("directory_checkpoint_id", sa.Uuid(), nullable=False),
        sa.Column("entry_name", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("directory_scope", sa.String(length=16), nullable=False),
        sa.Column("state_id", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("directory_scope = 'directory'", name="skill_member_scope_ck"),
        sa.CheckConstraint(
            "entry_name <> '' AND entry_name NOT IN ('ego-browser', 'agent-remote-device')",
            name="skill_member_name_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "directory_scope", "directory_checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
            ],
            name="skill_member_directory_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "state_id", "checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_member_checkpoint_fk",
        ),
        sa.PrimaryKeyConstraint("directory_checkpoint_id", "entry_name"),
        sa.UniqueConstraint(
            "directory_checkpoint_id", "state_id", name="skill_directory_member_state_uq"
        ),
    )
    op.create_table(
        "session_skill_snapshot_items",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("state_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("entry_name", sa.String(length=64), nullable=False),
        sa.Column("state_epoch", sa.BigInteger(), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), nullable=False),
        sa.Column(
            "resolution_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_name <> '' AND entry_name NOT IN ('ego-browser', 'agent-remote-device')",
            name="skill_snapshot_item_name_ck",
        ),
        sa.CheckConstraint("state_epoch >= 1", name="skill_snapshot_item_epoch_ck"),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "snapshot_id"],
            [
                "session_skill_snapshots.user_id",
                "session_skill_snapshots.account_id",
                "session_skill_snapshots.id",
            ],
            name="skill_snapshot_item_snapshot_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "state_id", "checkpoint_id"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.state_id",
                "skill_checkpoints.id",
            ],
            name="skill_snapshot_item_checkpoint_fk",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "state_id"),
        sa.UniqueConstraint("snapshot_id", "entry_name", name="skill_snapshot_item_name_uq"),
    )
    op.create_table(
        "skill_finalizations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("incoming_digest", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("tree_digest", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_scope", sa.String(length=16), nullable=False),
        sa.Column("checkpoint_id", sa.Uuid(), nullable=True),
        sa.Column("unclean", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'upload_pending' AND tree_digest IS NULL AND checkpoint_id IS "
            "NULL) OR (status <> 'upload_pending' AND tree_digest IS NOT NULL AND "
            "checkpoint_id IS NOT NULL AND tree_digest = incoming_digest)",
            name="skill_finalization_retained_ck",
        ),
        sa.CheckConstraint(
            "(unclean = false AND status <> 'persisted_unclean') OR (unclean = true "
            "AND status IN ('upload_pending', 'persisted_unclean', 'detached'))",
            name="skill_finalization_unclean_ck",
        ),
        sa.CheckConstraint(
            "category = 'state' AND checkpoint_scope = 'directory'",
            name="skill_finalization_scope_ck",
        ),
        sa.CheckConstraint(
            "status IN ('upload_pending', 'persisted', 'persisted_unclean', "
            "'published', 'conflicted', 'detached')",
            name="skill_finalization_status_ck",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "checkpoint_scope", "checkpoint_id", "tree_digest"],
            [
                "skill_checkpoints.user_id",
                "skill_checkpoints.account_id",
                "skill_checkpoints.scope",
                "skill_checkpoints.id",
                "skill_checkpoints.tree_digest",
            ],
            name="skill_finalization_checkpoint_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "account_id", "node_id", "snapshot_id"],
            [
                "session_skill_snapshots.user_id",
                "session_skill_snapshots.account_id",
                "session_skill_snapshots.node_id",
                "session_skill_snapshots.id",
            ],
            name="skill_finalization_snapshot_fk",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "category", "tree_digest"],
            [
                "skill_stored_trees.user_id",
                "skill_stored_trees.category",
                "skill_stored_trees.digest",
            ],
            name="skill_finalization_tree_fk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", name="skill_finalization_snapshot_uq"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="skill_finalization_key_uq"),
    )
    op.create_foreign_key(
        "skill_directory_head_fk",
        "account_skill_directory_states",
        "skill_checkpoints",
        ["user_id", "account_id", "head_scope", "head_checkpoint_id"],
        ["user_id", "account_id", "scope", "id"],
    )
    op.create_foreign_key(
        "skill_state_head_fk",
        "account_skill_states",
        "skill_checkpoints",
        ["user_id", "account_id", "id", "head_checkpoint_id"],
        ["user_id", "account_id", "state_id", "id"],
    )


def downgrade() -> None:
    """
    按引用逆序移除运行状态表，保留已有用户库与内容存储。
    """
    op.drop_constraint(
        "skill_directory_head_fk", "account_skill_directory_states", type_="foreignkey"
    )
    op.drop_constraint("skill_state_head_fk", "account_skill_states", type_="foreignkey")
    op.drop_table("skill_finalizations")
    op.drop_table("session_skill_snapshot_items")
    op.drop_table("skill_directory_members")
    op.drop_table("session_skill_snapshots")
    op.drop_table("skill_checkpoints")
    op.drop_table("account_skill_states")
    op.drop_table("account_skill_directory_states")
    op.drop_constraint("sessions_skill_binding_uq", "sessions", type_="unique")
    op.drop_constraint("node_tasks_owner_uq", "node_tasks", type_="unique")
