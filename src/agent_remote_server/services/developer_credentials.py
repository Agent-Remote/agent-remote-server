from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    DeveloperCredentialProfile,
    ToolAccount,
    ToolAccountDeveloperCredentialProfile,
    User,
)


class DeveloperCredentialService:
    """
    开发凭据 profile 服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        _ = settings

    async def list_profiles(self, *, user: User) -> list[DeveloperCredentialProfile]:
        """
        列出用户开发凭据 profile
        """

        result = await self._session.scalars(
            select(DeveloperCredentialProfile)
            .where(DeveloperCredentialProfile.user_id == user.id)
            .order_by(DeveloperCredentialProfile.created_at)
        )
        return list(result.all())

    async def create_profile(
        self,
        *,
        user: User,
        display_name: str,
        git_identity: dict[str, object],
        github_cli_mode: str,
        ssh_mode: str,
    ) -> DeveloperCredentialProfile:
        """
        创建开发凭据 profile
        """

        self._validate_modes(github_cli_mode=github_cli_mode, ssh_mode=ssh_mode)
        profile = DeveloperCredentialProfile(
            user_id=user.id,
            display_name=display_name,
            status="active",
            git_identity=self._clean_git_identity(git_identity),
            github_cli_mode=github_cli_mode,
            ssh_mode=ssh_mode,
            secret_ref=None,
        )
        self._session.add(profile)
        await self._session.flush()
        await self._audit(
            actor_user_id=user.id,
            action="developer_credentials.create",
            target_type="developer_credential_profile",
            target_id=str(profile.id),
            details={"github_cli_mode": github_cli_mode, "ssh_mode": ssh_mode},
        )
        await self._session.commit()
        return profile

    async def get_profile(self, *, user: User, profile_id: UUID) -> DeveloperCredentialProfile:
        """
        读取开发凭据 profile
        """

        profile = await self._require_profile(user=user, profile_id=profile_id)
        return profile

    async def update_profile(
        self,
        *,
        user: User,
        profile_id: UUID,
        display_name: str | None,
        status: str | None,
        git_identity: dict[str, object] | None,
        github_cli_mode: str | None,
        ssh_mode: str | None,
    ) -> DeveloperCredentialProfile:
        """
        更新开发凭据 profile
        """

        profile = await self._require_profile(user=user, profile_id=profile_id)
        if display_name is not None:
            profile.display_name = display_name
        if status is not None:
            if status not in {"active", "disabled"}:
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message="Invalid developer credential status.",
                    status_code=422,
                )
            profile.status = status
        if git_identity is not None:
            profile.git_identity = self._clean_git_identity(git_identity)
        if github_cli_mode is not None:
            self._validate_modes(github_cli_mode=github_cli_mode, ssh_mode=profile.ssh_mode)
            profile.github_cli_mode = github_cli_mode
        if ssh_mode is not None:
            self._validate_modes(github_cli_mode=profile.github_cli_mode, ssh_mode=ssh_mode)
            profile.ssh_mode = ssh_mode
        await self._audit(
            actor_user_id=user.id,
            action="developer_credentials.update",
            target_type="developer_credential_profile",
            target_id=str(profile.id),
            details={},
        )
        await self._session.commit()
        return profile

    async def disable_profile(self, *, user: User, profile_id: UUID) -> DeveloperCredentialProfile:
        """
        禁用开发凭据 profile
        """

        profile = await self._require_profile(user=user, profile_id=profile_id)
        profile.status = "disabled"
        await self._audit(
            actor_user_id=user.id,
            action="developer_credentials.disable",
            target_type="developer_credential_profile",
            target_id=str(profile.id),
            details={},
        )
        await self._session.commit()
        return profile

    async def bind_to_tool_account(
        self,
        *,
        user: User,
        account_id: UUID,
        profile_id: UUID,
    ) -> DeveloperCredentialProfile:
        """
        将开发凭据 profile 绑定到工具账户
        """

        account = await self._require_account(user=user, account_id=account_id)
        profile = await self._require_profile(user=user, profile_id=profile_id)
        if profile.status != "active":
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Developer credential profile is not active.",
                status_code=422,
            )
        existing = await self._session.scalar(
            select(ToolAccountDeveloperCredentialProfile).where(
                ToolAccountDeveloperCredentialProfile.tool_account_id == account.id
            )
        )
        if existing is None:
            self._session.add(
                ToolAccountDeveloperCredentialProfile(
                    tool_account_id=account.id,
                    developer_credential_profile_id=profile.id,
                )
            )
        else:
            existing.developer_credential_profile_id = profile.id
        await self._audit(
            actor_user_id=user.id,
            action="developer_credentials.bind",
            target_type="tool_account",
            target_id=str(account.id),
            details={"profile_id": str(profile.id)},
        )
        await self._session.commit()
        return profile

    async def unbind_from_tool_account(self, *, user: User, account_id: UUID) -> None:
        """
        解除工具账户开发凭据 profile 绑定
        """

        account = await self._require_account(user=user, account_id=account_id)
        existing = await self._session.scalar(
            select(ToolAccountDeveloperCredentialProfile).where(
                ToolAccountDeveloperCredentialProfile.tool_account_id == account.id
            )
        )
        if existing is not None:
            await self._session.delete(existing)
        await self._audit(
            actor_user_id=user.id,
            action="developer_credentials.unbind",
            target_type="tool_account",
            target_id=str(account.id),
            details={},
        )
        await self._session.commit()

    async def _require_profile(self, *, user: User, profile_id: UUID) -> DeveloperCredentialProfile:
        profile = await self._session.get(DeveloperCredentialProfile, profile_id)
        if profile is None or profile.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Developer credential profile was not found.",
                status_code=404,
            )
        return profile

    async def _require_account(self, *, user: User, account_id: UUID) -> ToolAccount:
        account = await self._session.get(ToolAccount, account_id)
        if account is None or account.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Tool account was not found.",
                status_code=404,
            )
        return account

    def _clean_git_identity(self, git_identity: dict[str, object]) -> dict[str, object]:
        allowed: dict[str, object] = {}
        for key in ("user_name", "user_email"):
            value = git_identity.get(key)
            if isinstance(value, str) and value:
                allowed[key] = value
        return allowed

    def _validate_modes(self, *, github_cli_mode: str, ssh_mode: str) -> None:
        if github_cli_mode not in {"remote_login", "import_token", "disabled"}:
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Invalid GitHub CLI mode.",
                status_code=422,
            )
        if ssh_mode not in {"agent_forwarding", "deploy_key", "disabled"}:
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Invalid SSH credential mode.",
                status_code=422,
            )

    async def _audit(
        self,
        *,
        actor_user_id: UUID,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, object],
    ) -> None:
        self._session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )
