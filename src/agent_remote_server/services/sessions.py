from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    DeveloperCredentialProfile,
    Node,
    NodeTask,
    Session,
    ToolAccount,
    User,
)
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.sessions import SessionRepository
from agent_remote_server.services.tool_accounts import ACCOUNT_CONFIG_ROOT, ACTIVE_NODE_STATUSES
from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate

ACTIVE_SESSION_STATUSES = {"starting", "running", "active"}


class ToolSessionService:
    """
    工具运行 session 生命周期服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = SessionRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._registry = ToolRegistry()

    async def list_sessions(self, *, user: User, tool_type: str | None) -> list[Session]:
        """
        列出用户工具 session

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :return list: 工具 session 列表
        """

        return list(await self._repository.list_sessions_for_user(user.id, tool_type))

    async def get_current_project_session(
        self, *, user: User, tool_type: str, project_key: str
    ) -> Session:
        """
        读取当前项目最近可恢复工具 session

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :param project_key (str): 项目 key
        :return Session: 工具 session 实体
        """

        session = await self._repository.get_latest_project_session(
            user_id=user.id, tool_type=tool_type, project_key=project_key
        )
        if session is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        return session

    async def get_session(self, *, user: User, session_id: UUID) -> Session:
        """
        读取当前用户工具 session

        :param user (User): 当前用户
        :param session_id (UUID): session ID
        :return Session: 工具 session 实体
        """

        return await self._require_user_session(user=user, session_id=session_id)

    async def create_session(
        self,
        *,
        user: User,
        tool_type: str,
        tool_account_id: UUID,
        workspace_id: UUID,
        project_key: str,
        argv: list[str],
        replaces_session_id: UUID | None = None,
    ) -> Session:
        """
        创建工具运行 session 并投递节点任务

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :param tool_account_id (UUID): 工具账户 ID
        :param workspace_id (UUID): workspace ID
        :param project_key (str): 项目 key
        :param argv (list): 工具 CLI 透传参数
        :param replaces_session_id (UUID | None): 被替代的中断会话 ID
        :return Session: 工具 session 实体
        """

        template = self._registry.get(tool_type)
        account = await self._require_active_account(
            user=user, tool_type=template.tool_type, account_id=tool_account_id
        )
        workspace = await self._repository.get_workspace(workspace_id)
        if workspace is None or workspace.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Workspace was not found.", status_code=404
            )
        if workspace.project_key != project_key:
            raise ApiError(
                code="PROJECT_KEY_MISMATCH",
                message="Workspace project key does not match request.",
                status_code=409,
            )
        replaced_session = None
        if replaces_session_id is not None:
            replaced_session = await self._repository.get_session(replaces_session_id)
            if (
                replaced_session is None
                or replaced_session.user_id != user.id
                or replaced_session.status != "interrupted"
                or replaced_session.tool_account_id != account.id
                or replaced_session.workspace_id != workspace.id
                or replaced_session.project_key != project_key
            ):
                raise ApiError(
                    code="SESSION_REPLACEMENT_INVALID",
                    message="Only a matching interrupted session can be replaced.",
                    status_code=409,
                )
        if not workspace.remote_path:
            raise ApiError(
                code="WORKSPACE_NOT_PREPARED",
                message="Workspace has no remote path.",
                status_code=409,
            )
        node = await self._choose_session_node(account)
        if account.runtime_backend is None:
            account.runtime_backend = node.default_runtime_backend
        runtime_backend = account.runtime_backend
        profile = await self._repository.get_account_profile(account.id)
        developer_profile = await self._repository.get_developer_credential_profile_for_account(
            account.id
        )
        account_remote_path = self._profile_text(
            profile.profile_json if profile is not None else {},
            "account_remote_path",
            self._account_remote_path(user.id, account.tool_type, account.id),
        )
        developer_credential_profile_path = (
            self._developer_credential_profile_path(user.id, developer_profile.id)
            if developer_profile is not None
            else None
        )
        tool_session = await self._repository.add_session(
            Session(
                tool_type=template.tool_type,
                user_id=user.id,
                tool_account_id=account.id,
                workspace_id=workspace.id,
                node_id=node.id,
                project_key=project_key,
                status="starting",
                tmux_session_name=None,
                container_id=None,
                runtime_backend=runtime_backend,
                runtime_resource_id=None,
                replaces_session_id=replaced_session.id if replaced_session is not None else None,
            )
        )
        tmux_session_name = self._tmux_session_name(tool_session)
        sandbox_name = self._sandbox_name(tool_session)
        tool_session.tmux_session_name = tmux_session_name
        tool_session.container_id = sandbox_name
        account.affinity_node_id = node.id
        task_id = f"create_tool_session:{tool_session.id}"
        await self._repository.add_task(
            NodeTask(
                node_id=node.id,
                task_id=task_id,
                task_type="create_tool_session",
                status="pending",
                payload={
                    "session_id": str(tool_session.id),
                    "tool_account_id": str(account.id),
                    "tool_type": template.tool_type,
                    "user_id": str(user.id),
                    "workspace_id": str(workspace.id),
                    "project_key": project_key,
                    "workspace_remote_path": workspace.remote_path,
                    "account_remote_path": account_remote_path,
                    "developer_credential_profile_path": developer_credential_profile_path,
                    "developer_credentials": self._developer_credentials_payload(developer_profile),
                    "sync_git": workspace.sync_git,
                    "git_sync_policy": workspace.git_sync_policy,
                    "tmux_session_name": tmux_session_name,
                    "sandbox_name": sandbox_name,
                    "timezone": account.timezone,
                    "locale": account.locale,
                    "argv": list(argv),
                    "template": self._runtime_payload(template, argv),
                    "runtime_backend": runtime_backend,
                    "runtime_policy": node.runtime_policy,
                },
                retry_count=0,
            )
        )
        await self._audit(
            actor_user_id=user.id,
            action="sessions.create",
            target_type="session",
            target_id=str(tool_session.id),
            details={"node_id": str(node.id), "task_id": task_id},
        )
        await self._session.commit()
        return tool_session

    async def stop_session(self, *, user: User, session_id: UUID) -> Session:
        """
        停止工具运行 session

        :param user (User): 当前用户
        :param session_id (UUID): 工具 session ID
        :return Session: 工具 session 实体
        """

        tool_session = await self._require_user_session(user=user, session_id=session_id)
        if tool_session.status in {"stopped", "failed"}:
            return tool_session
        task_id = f"stop_tool_session:{tool_session.id}"
        existing = await self._repository.get_task_by_task_id(task_id)
        if existing is None:
            await self._repository.add_task(
                NodeTask(
                    node_id=tool_session.node_id,
                    task_id=task_id,
                    task_type="stop_tool_session",
                    status="pending",
                    payload={
                        "session_id": str(tool_session.id),
                        "tmux_session_name": tool_session.tmux_session_name,
                        "sandbox_name": tool_session.container_id,
                        "runtime_backend": tool_session.runtime_backend,
                        "runtime_resource_id": tool_session.runtime_resource_id,
                    },
                    retry_count=0,
                )
            )
        tool_session.status = "stopping"
        await self._audit(
            actor_user_id=user.id,
            action="sessions.stop",
            target_type="session",
            target_id=str(tool_session.id),
            details={"task_id": task_id},
        )
        await self._session.commit()
        return tool_session

    async def _require_user_session(self, *, user: User, session_id: UUID) -> Session:
        tool_session = await self._repository.get_session(session_id)
        if tool_session is None or tool_session.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        return tool_session

    async def _require_active_account(
        self, *, user: User, tool_type: str, account_id: UUID
    ) -> ToolAccount:
        account = await self._repository.get_account(account_id)
        if account is None or account.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Tool account was not found.", status_code=404
            )
        if account.tool_type != tool_type:
            raise ApiError(
                code="TOOL_ACCOUNT_MISMATCH",
                message="Tool account type does not match requested tool.",
                status_code=409,
            )
        if account.status != "active":
            raise ApiError(
                code="TOOL_ACCOUNT_NOT_ACTIVE",
                message="Tool account is not active.",
                status_code=409,
            )
        return account

    async def _choose_session_node(self, account: ToolAccount) -> Node:
        active_sessions = await self._repository.list_active_sessions_for_account(account.id)
        if active_sessions:
            node = await self._repository.get_node(active_sessions[0].node_id)
            if node is not None and self._node_can_host(node, account):
                return node
            raise ApiError(
                code="NODE_UNAVAILABLE",
                message="Active sessions for this account are pinned to an unavailable node.",
                status_code=409,
            )
        if account.affinity_node_id is not None:
            node = await self._repository.get_node(account.affinity_node_id)
            if node is not None and self._node_can_host(node, account):
                return node
        candidates = await self._repository.list_candidate_nodes(
            tool_type=account.tool_type,
            region_code=account.region_code,
            preferred_tags=account.preferred_node_tags,
        )
        node = next(
            (candidate for candidate in candidates if self._node_can_host(candidate, account)), None
        )
        if node is None:
            raise ApiError(
                code="NODE_UNAVAILABLE",
                message="No available node can host this tool session.",
                status_code=409,
            )
        return node

    def _node_can_host(self, node: Node | None, account: ToolAccount) -> bool:
        if node is None or node.status not in ACTIVE_NODE_STATUSES:
            return False
        if account.tool_type not in node.supported_tool_types:
            return False
        if node.region_code != account.region_code:
            return False
        backend = account.runtime_backend or node.default_runtime_backend
        if backend not in node.allowed_runtime_backends:
            return False
        available = node.runtime_capabilities.get("backends")
        if isinstance(available, list):
            return backend in available
        return backend == "docker_sandbox"

    def _runtime_payload(self, template: ToolRuntimeTemplate, argv: list[str]) -> dict[str, object]:
        command = [template.sandbox_agent, *argv]
        return {
            "sandbox_agent": template.sandbox_agent,
            "command": command,
            "verifier": template.verifier,
        }

    def _tmux_session_name(self, tool_session: Session) -> str:
        return f"ar-{tool_session.tool_type}-{str(tool_session.id).replace('-', '')[:24]}"

    def _sandbox_name(self, tool_session: Session) -> str:
        return f"agent-remote-{tool_session.tool_type}-{str(tool_session.id).replace('-', '')[:24]}"

    def _account_remote_path(self, user_id: UUID, tool_type: str, account_id: UUID) -> str:
        return f"{ACCOUNT_CONFIG_ROOT}/{user_id}/tool-accounts/{tool_type}/{account_id}"

    def _developer_credential_profile_path(self, user_id: UUID, profile_id: UUID) -> str:
        return f"{ACCOUNT_CONFIG_ROOT}/{user_id}/developer-credential-profiles/{profile_id}"

    def _developer_credentials_payload(
        self, profile: DeveloperCredentialProfile | None
    ) -> dict[str, object] | None:
        if profile is None:
            return None
        return {
            "profile_id": str(profile.id),
            "git_identity": profile.git_identity,
            "gh_mode": profile.github_cli_mode,
            "ssh_mode": profile.ssh_mode,
        }

    def _profile_text(self, profile_json: dict[str, object], key: str, default: str) -> str:
        value = profile_json.get(key)
        if isinstance(value, str) and value:
            return value
        return default

    async def _audit(
        self,
        *,
        actor_user_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, object],
    ) -> None:
        await self._identity_repository.add_audit_log(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )
