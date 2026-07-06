"""add workspace git sync policy

Revision ID: 0006_workspace_git_sync_policy
Revises: 0005_tool_account_binding
Create Date: 2026-07-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_workspace_git_sync_policy"
down_revision: str | None = "0005_tool_account_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DEFAULT_GIT_POLICY = {
    "exclude_hooks": True,
    "exclude_locks": True,
    "require_clean_git_lock": True,
    "warn_concurrent_git": True,
}

DEFAULT_EXCLUDES = [
    ".git/**/*.lock",
    ".git/hooks",
    ".git/worktrees",
    "node_modules",
    "target",
    "dist",
    ".venv",
    "__pycache__",
]


def upgrade() -> None:
    op.create_table(
        "developer_credential_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("git_identity", sa.JSON(), nullable=False),
        sa.Column("github_cli_mode", sa.String(length=32), nullable=False),
        sa.Column("ssh_mode", sa.String(length=32), nullable=False),
        sa.Column("secret_ref", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status in ('active', 'disabled')",
            name="developer_credential_profiles_status_ck",
        ),
        sa.CheckConstraint(
            "github_cli_mode in ('remote_login', 'import_token', 'disabled')",
            name="developer_credential_profiles_gh_mode_ck",
        ),
        sa.CheckConstraint(
            "ssh_mode in ('agent_forwarding', 'deploy_key', 'disabled')",
            name="developer_credential_profiles_ssh_mode_ck",
        ),
    )
    op.create_index(
        "developer_credential_profiles_user_idx",
        "developer_credential_profiles",
        ["user_id"],
    )
    op.create_table(
        "tool_account_developer_credential_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tool_account_id", sa.UUID(), nullable=False),
        sa.Column("developer_credential_profile_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tool_account_id"], ["tool_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["developer_credential_profile_id"],
            ["developer_credential_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tool_account_id",
            name="tool_account_developer_credential_profiles_account_uq",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("sync_git", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "workspaces",
        sa.Column("git_sync_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "sync_sessions",
        sa.Column("sync_git", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "sync_sessions",
        sa.Column("exclude_patterns", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    workspaces = sa.table(
        "workspaces",
        sa.column("git_sync_policy", sa.JSON()),
    )
    sync_sessions = sa.table(
        "sync_sessions",
        sa.column("exclude_patterns", sa.JSON()),
    )
    op.execute(workspaces.update().values(git_sync_policy=DEFAULT_GIT_POLICY))
    op.execute(sync_sessions.update().values(exclude_patterns=DEFAULT_EXCLUDES))
    op.alter_column("workspaces", "sync_git", server_default=None)
    op.alter_column("workspaces", "git_sync_policy", server_default=None)
    op.alter_column("sync_sessions", "sync_git", server_default=None)
    op.alter_column("sync_sessions", "exclude_patterns", server_default=None)


def downgrade() -> None:
    op.drop_column("sync_sessions", "exclude_patterns")
    op.drop_column("sync_sessions", "sync_git")
    op.drop_column("workspaces", "git_sync_policy")
    op.drop_column("workspaces", "sync_git")
    op.drop_table("tool_account_developer_credential_profiles")
    op.drop_index(
        "developer_credential_profiles_user_idx",
        table_name="developer_credential_profiles",
    )
    op.drop_table("developer_credential_profiles")
