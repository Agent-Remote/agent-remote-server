from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    Node,
    NodeTask,
    ToolAccount,
    ToolAccountProfile,
    User,
)
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.tool_accounts import ToolAccountRepository
from agent_remote_server.schemas.tool_accounts import BindingStatusData
from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate

ACTIVE_NODE_STATUSES = {"healthy", "degraded"}
ACCOUNT_CONFIG_ROOT = "/var/lib/agent-remote/users"


@dataclass(frozen=True)
class BindingSession:
    """
    工具账户绑定会话
    """

    status: BindingStatusData
    task: NodeTask | None


class ToolAccountService:
    """
    工具账户管理和绑定服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = ToolAccountRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._registry = ToolRegistry()

    async def create_account(
        self,
        *,
        user: User,
        tool_type: str,
        display_name: str,
        region_code: str,
        timezone: str,
        locale: str,
        preferred_node_tags: list[str],
    ) -> ToolAccount:
        """
        创建工具账户

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :param display_name (str): 显示名称
        :param region_code (str): 地区代码
        :param timezone (str): 时区
        :param locale (str): 区域设置
        :param preferred_node_tags (list): 偏好节点标签

        :return ToolAccount: 工具账户实体
        """

        template = self._require_template(tool_type)
        account = await self._repository.add_account(
            ToolAccount(
                user_id=user.id,
                tool_type=template.tool_type,
                display_name=display_name,
                status="binding_requested",
                region_code=region_code,
                timezone=timezone,
                locale=locale,
                preferred_node_tags=preferred_node_tags,
            )
        )
        await self._repository.add_profile(
            ToolAccountProfile(
                tool_account_id=account.id,
                tool_type=account.tool_type,
                profile_json={
                    "tool_type": account.tool_type,
                    "account_remote_path": self._account_remote_path(user.id, account.id),
                    "config_subdir": template.account_config_subdir,
                    "local_cli_secrets": False,
                },
                encrypted_secrets=None,
            )
        )
        await self._audit(
            actor_user_id=user.id,
            action="tool_accounts.create",
            target_type="tool_account",
            target_id=str(account.id),
            details={"tool_type": account.tool_type, "region_code": region_code},
        )
        await self._session.commit()
        return account

    async def list_accounts(self, *, user: User) -> list[ToolAccount]:
        """
        列出当前用户工具账户

        :param user (User): 当前用户

        :return list: 工具账户列表
        """

        return list(await self._repository.list_accounts_for_user(user.id))

    async def get_account(self, *, user: User, account_id: UUID) -> ToolAccount:
        """
        读取当前用户工具账户

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID

        :return ToolAccount: 工具账户实体
        """

        account = await self._require_account(user=user, account_id=account_id)
        return account

    async def update_account(
        self,
        *,
        user: User,
        account_id: UUID,
        display_name: str | None,
        status: str | None,
        region_code: str | None,
        timezone: str | None,
        locale: str | None,
        preferred_node_tags: list[str] | None,
    ) -> ToolAccount:
        """
        更新工具账户

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID
        :param display_name (str): 显示名称
        :param status (str): 账户状态
        :param region_code (str): 地区代码
        :param timezone (str): 时区
        :param locale (str): 区域设置
        :param preferred_node_tags (list): 偏好节点标签

        :return ToolAccount: 工具账户实体
        """

        account = await self._require_account(user=user, account_id=account_id)
        if display_name is not None:
            account.display_name = display_name
        if status is not None:
            self._validate_manual_status(status)
            account.status = status
        if region_code is not None:
            account.region_code = region_code
        if timezone is not None:
            account.timezone = timezone
        if locale is not None:
            account.locale = locale
        if preferred_node_tags is not None:
            account.preferred_node_tags = preferred_node_tags
        await self._audit(
            actor_user_id=user.id,
            action="tool_accounts.update",
            target_type="tool_account",
            target_id=str(account.id),
            details={"status": status} if status else {},
        )
        await self._session.commit()
        return account

    async def disable_account(self, *, user: User, account_id: UUID) -> ToolAccount:
        """
        禁用工具账户

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID

        :return ToolAccount: 工具账户实体
        """

        account = await self._require_account(user=user, account_id=account_id)
        account.status = "disabled"
        await self._audit(
            actor_user_id=user.id,
            action="tool_accounts.disable",
            target_type="tool_account",
            target_id=str(account.id),
            details={},
        )
        await self._session.commit()
        return account

    async def start_binding(self, *, user: User, account_id: UUID) -> BindingSession:
        """
        创建工具账户绑定任务

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID

        :return BindingSession: 绑定会话
        """

        account = await self._require_account(user=user, account_id=account_id)
        template = self._require_template(account.tool_type)
        node = await self._choose_binding_node(account)
        profile = await self._ensure_profile(account=account, template=template)

        account_remote_path = self._profile_text(
            profile.profile_json,
            "account_remote_path",
            self._account_remote_path(user.id, account.id),
        )
        binding_session_id = self._profile_text(
            profile.profile_json,
            "binding_session_id",
            f"bind-{account.id}",
        )
        tmux_session_name = self._profile_text(
            profile.profile_json,
            "tmux_session_name",
            self._tmux_session_name(account),
        )
        task_id = f"create_binding_session:{account.id}"

        account.affinity_node_id = node.id
        account.status = "binding_session_starting"
        profile.profile_json = {
            **profile.profile_json,
            "binding_session_id": binding_session_id,
            "tmux_session_name": tmux_session_name,
            "account_remote_path": account_remote_path,
            "template": self._template_payload(template),
            "verifier": template.verifier,
            "local_cli_secrets": False,
        }
        task = await self._repository.get_task_by_task_id(task_id)
        if task is None:
            task = await self._repository.add_task(
                NodeTask(
                    node_id=node.id,
                    task_id=task_id,
                    task_type="create_binding_session",
                    status="pending",
                    payload={
                        "binding_id": binding_session_id,
                        "tool_account_id": str(account.id),
                        "tool_type": account.tool_type,
                        "user_id": str(user.id),
                        "region_code": account.region_code,
                        "timezone": account.timezone,
                        "locale": account.locale,
                        "account_remote_path": account_remote_path,
                        "tmux_session_name": tmux_session_name,
                        "template": self._template_payload(template),
                        "verifier": template.verifier,
                    },
                    retry_count=0,
                )
            )
        await self._audit(
            actor_user_id=user.id,
            action="tool_accounts.bind.start",
            target_type="tool_account",
            target_id=str(account.id),
            details={"node_id": str(node.id), "task_id": task.task_id},
        )
        await self._session.commit()
        return BindingSession(status=self._binding_status(account, profile, node, task), task=task)

    async def get_binding_status(self, *, user: User, account_id: UUID) -> BindingStatusData:
        """
        读取工具账户绑定状态

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID

        :return BindingStatusData: 绑定状态数据
        """

        account = await self._require_account(user=user, account_id=account_id)
        profile = await self._repository.get_profile(account.id)
        node = await self._load_affinity_node(account)
        task = await self._repository.get_task_by_task_id(f"create_binding_session:{account.id}")
        return self._binding_status(account, profile, node, task)

    async def verify_binding(self, *, user: User, account_id: UUID) -> BindingSession:
        """
        创建工具账户校验任务

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID

        :return BindingSession: 绑定会话
        """

        account = await self._require_account(user=user, account_id=account_id)
        template = self._require_template(account.tool_type)
        profile = await self._ensure_profile(account=account, template=template)
        node = await self._load_affinity_node(account)
        if node is None or node.status not in ACTIVE_NODE_STATUSES:
            raise ApiError(
                code="NODE_UNAVAILABLE",
                message="Tool account does not have an available affinity node.",
                status_code=409,
            )

        account.status = "binding_verifying"
        task_id = f"verify_tool_account:{account.id}"
        account_remote_path = self._profile_text(
            profile.profile_json,
            "account_remote_path",
            self._account_remote_path(user.id, account.id),
        )
        task = await self._repository.get_task_by_task_id(task_id)
        if task is None:
            task = await self._repository.add_task(
                NodeTask(
                    node_id=node.id,
                    task_id=task_id,
                    task_type="verify_tool_account",
                    status="pending",
                    payload={
                        "tool_account_id": str(account.id),
                        "tool_type": account.tool_type,
                        "user_id": str(user.id),
                        "verifier": template.verifier,
                        "account_remote_path": account_remote_path,
                    },
                    retry_count=0,
                )
            )
        await self._audit(
            actor_user_id=user.id,
            action="tool_accounts.bind.verify",
            target_type="tool_account",
            target_id=str(account.id),
            details={"node_id": str(node.id), "task_id": task.task_id},
        )
        await self._session.commit()
        return BindingSession(status=self._binding_status(account, profile, node, task), task=task)

    async def _choose_binding_node(self, account: ToolAccount) -> Node:
        node = await self._load_affinity_node(account)
        if node is not None and self._node_can_host(node, account):
            return node

        candidates = await self._repository.list_candidate_nodes(
            tool_type=account.tool_type,
            region_code=account.region_code,
            preferred_tags=account.preferred_node_tags,
        )
        if not candidates:
            account.status = "node_unavailable"
            await self._session.flush()
            raise ApiError(
                code="NODE_UNAVAILABLE",
                message="No available node can host this tool account.",
                status_code=409,
            )
        return candidates[0]

    async def _load_affinity_node(self, account: ToolAccount) -> Node | None:
        if account.affinity_node_id is None:
            return None
        return await self._repository.get_node(account.affinity_node_id)

    async def _ensure_profile(
        self, *, account: ToolAccount, template: ToolRuntimeTemplate
    ) -> ToolAccountProfile:
        profile = await self._repository.get_profile(account.id)
        if profile is not None:
            return profile
        return await self._repository.add_profile(
            ToolAccountProfile(
                tool_account_id=account.id,
                tool_type=account.tool_type,
                profile_json={
                    "tool_type": account.tool_type,
                    "account_remote_path": self._account_remote_path(account.user_id, account.id),
                    "config_subdir": template.account_config_subdir,
                    "local_cli_secrets": False,
                },
                encrypted_secrets=None,
            )
        )

    async def _require_account(self, *, user: User, account_id: UUID) -> ToolAccount:
        account = await self._repository.get_account(account_id)
        if account is None or account.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Tool account was not found.",
                status_code=404,
            )
        return account

    def _binding_status(
        self,
        account: ToolAccount,
        profile: ToolAccountProfile | None,
        node: Node | None,
        task: NodeTask | None,
    ) -> BindingStatusData:
        profile_json = profile.profile_json if profile is not None else {}
        return BindingStatusData(
            tool_account_id=account.id,
            status=account.status,
            node_id=node.id if node is not None else account.affinity_node_id,
            binding_session_id=self._optional_text(profile_json, "binding_session_id"),
            tmux_session_name=self._optional_text(profile_json, "tmux_session_name"),
            account_remote_path=self._optional_text(profile_json, "account_remote_path"),
            connect_command=self._connect_command(node, profile_json),
            task_id=task.task_id if task is not None else None,
            verifier=self._optional_text(profile_json, "verifier"),
            error=self._optional_text(profile_json, "last_error"),
        )

    def _connect_command(self, node: Node | None, profile_json: dict[str, object]) -> str | None:
        if node is None:
            return None
        tmux_session_name = self._optional_text(profile_json, "tmux_session_name")
        if tmux_session_name is None:
            return None
        ssh_host = node.ssh_host or node.wireguard_ip
        if ssh_host is None:
            return None
        ssh_user = node.ssh_user or "agent-remote"
        ssh_port = node.ssh_port or 22
        return f"ssh -p {ssh_port} {ssh_user}@{ssh_host} tmux attach-session -t {tmux_session_name}"

    def _node_can_host(self, node: Node | None, account: ToolAccount) -> bool:
        if node is None:
            return False
        if node.status not in ACTIVE_NODE_STATUSES:
            return False
        if account.tool_type not in node.supported_tool_types:
            return False
        return node.region_code == account.region_code

    def _require_template(self, tool_type: str) -> ToolRuntimeTemplate:
        return self._registry.get(tool_type)

    def _validate_manual_status(self, status: str) -> None:
        if status not in {"binding_requested", "expired", "disabled"}:
            raise ApiError(
                code="COMMON_BAD_REQUEST",
                message="Status cannot be set manually.",
                status_code=400,
            )

    def _template_payload(self, template: ToolRuntimeTemplate) -> dict[str, object]:
        return {
            "sandbox_agent": template.sandbox_agent,
            "command": list(template.command),
            "verifier": template.verifier,
        }

    def _tmux_session_name(self, account: ToolAccount) -> str:
        return f"ar-bind-{str(account.id).replace('-', '')[:24]}"

    def _account_remote_path(self, user_id: UUID, account_id: UUID) -> str:
        return f"{ACCOUNT_CONFIG_ROOT}/{user_id}/accounts/{account_id}"

    def _profile_text(self, profile_json: dict[str, object], key: str, default: str) -> str:
        value = profile_json.get(key)
        if isinstance(value, str) and value:
            return value
        return default

    def _optional_text(self, profile_json: dict[str, object], key: str) -> str | None:
        value = profile_json.get(key)
        if isinstance(value, str) and value:
            return value
        return None

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
