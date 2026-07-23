from uuid import UUID

from sqlalchemy import JSON as JsonType
from sqlalchemy import ForeignKey, Index, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin


class ToolAccount(IdMixin, TimestampMixin, Base):
    """
    通用工具账户
    """

    __tablename__ = "tool_accounts"
    __table_args__ = (
        Index("tool_accounts_user_tool_idx", "user_id", "tool_type"),
        Index("tool_accounts_affinity_node_idx", "affinity_node_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    locale: Mapped[str] = mapped_column(String(64), nullable=False)
    preferred_node_tags: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    affinity_node_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    runtime_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ToolAccountProfile(IdMixin, TimestampMixin, Base):
    """
    工具账户专属配置
    """

    __tablename__ = "tool_account_profiles"

    tool_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_json: Mapped[dict[str, object]] = mapped_column(JsonType, nullable=False, default=dict)
    encrypted_secrets: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class DeveloperCredentialProfile(IdMixin, TimestampMixin, Base):
    """
    开发凭据 profile
    """

    __tablename__ = "developer_credential_profiles"
    __table_args__ = (Index("developer_credential_profiles_user_idx", "user_id"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    git_identity: Mapped[dict[str, object]] = mapped_column(JsonType, nullable=False, default=dict)
    github_cli_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="remote_login")
    ssh_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="agent_forwarding")
    secret_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)


class ToolAccountDeveloperCredentialProfile(IdMixin, TimestampMixin, Base):
    """
    工具账户和开发凭据 profile 绑定
    """

    __tablename__ = "tool_account_developer_credential_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tool_account_id",
            name="tool_account_developer_credential_profiles_account_uq",
        ),
    )

    tool_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    developer_credential_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("developer_credential_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
