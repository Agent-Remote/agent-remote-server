import base64
import binascii
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    Node,
    NodeTask,
    ToolAccount,
    ToolAccountProfile,
    User,
)
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.tool_accounts import ToolAccountRepository
from agent_remote_server.schemas.tool_accounts import (
    BindingStatusData,
    RuntimeMigrationData,
    ToolAccountConfigImportData,
    ToolAccountConfigImportFile,
)
from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate

ACTIVE_NODE_STATUSES = {"healthy", "degraded"}
ACCOUNT_CONFIG_ROOT = "/var/lib/agent-remote/users"
DEFAULT_CONFIG_IMPORT_PATHS = {
    "~/.claude/settings.json",
    "~/.claude/CLAUDE.md",
    "~/.claude/agents",
    "~/.claude/skills",
}
ASK_CONFIG_IMPORT_PREFIXES = (
    "~/.claude/plugins",
    "~/.claude/hooks",
    "~/.claude/rules",
)
RESUME_HISTORY_PATHS = {
    "~/.claude/projects",
    "~/.claude/sessions",
    "~/.claude/history.jsonl",
    "~/.claude/file-history",
    "~/.claude/plans",
    "~/.claude/tasks",
    "~/.claude/session-env",
}
DENIED_CONFIG_IMPORT_PATHS = {
    "~/.claude.json",
    "~/.claude/cache",
    "~/.claude/logs",
    "~/.claude/transcripts",
    "~/.claude/paste-cache",
    "~/.claude/stats-cache.json",
    "~/.claude/mcp-needs-auth-cache.json",
}
CONFIG_IMPORT_MAX_FILE_BYTES = 1024 * 1024
CONFIG_IMPORT_MAX_TOTAL_BYTES = 8 * 1024 * 1024


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
                    "account_remote_path": self._account_remote_path(
                        user.id, account.tool_type, account.id
                    ),
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

    async def migrate_runtime(
        self, *, actor: User, account_id: UUID, target_backend: str
    ) -> RuntimeMigrationData:
        """
        校验并派发工具账户运行时迁移任务

        :param actor (User): 当前管理员
        :param account_id (UUID): 工具账户 ID
        :param target_backend (str): 目标运行时

        :return RuntimeMigrationData: 迁移任务数据
        """

        account = await self._repository.get_account(account_id)
        if account is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Tool account was not found.", status_code=404
            )
        source_backend = account.runtime_backend
        if source_backend not in {"docker_sandbox", "native"}:
            raise ApiError(
                code="RUNTIME_BACKEND_NOT_PINNED",
                message="Tool account does not have a pinned runtime backend.",
                status_code=409,
            )
        if target_backend not in {"docker_sandbox", "native"} or target_backend == source_backend:
            raise ApiError(
                code="RUNTIME_MIGRATION_INVALID_TARGET",
                message="Target runtime backend is invalid or unchanged.",
                status_code=409,
            )
        if await self._repository.list_active_sessions(account.id):
            raise ApiError(
                code="RUNTIME_MIGRATION_ACTIVE_SESSIONS",
                message="Stop all active sessions before migrating the account.",
                status_code=409,
            )
        if account.affinity_node_id is None:
            raise ApiError(
                code="RUNTIME_MIGRATION_NODE_MISSING",
                message="Tool account has no affinity node.",
                status_code=409,
            )
        node = await self._repository.get_node(account.affinity_node_id)
        if node is None or target_backend not in node.allowed_runtime_backends:
            raise ApiError(
                code="RUNTIME_BACKEND_UNAVAILABLE",
                message="Target runtime is not allowed on the account node.",
                status_code=409,
            )
        available = node.runtime_capabilities.get("backends")
        if not isinstance(available, list) or target_backend not in available:
            raise ApiError(
                code="RUNTIME_BACKEND_UNAVAILABLE",
                message="Target runtime capability is unavailable on the account node.",
                status_code=409,
            )
        profile = await self._ensure_profile(
            account=account, template=self._require_template(account.tool_type)
        )
        previous_status = account.status
        task_id = f"migrate_tool_account_runtime:{account.id}:{uuid4()}"
        await self._repository.add_task(
            NodeTask(
                node_id=node.id,
                task_id=task_id,
                task_type="migrate_tool_account_runtime",
                status="pending",
                payload={
                    "tool_account_id": str(account.id),
                    "user_id": str(account.user_id),
                    "tool_type": account.tool_type,
                    "source_runtime_backend": source_backend,
                    "target_runtime_backend": target_backend,
                    "runtime_policy": node.runtime_policy,
                },
                retry_count=0,
            )
        )
        account.status = "migrating"
        profile.profile_json = {
            **profile.profile_json,
            "runtime_migration": {
                "task_id": task_id,
                "source_runtime_backend": source_backend,
                "target_runtime_backend": target_backend,
                "previous_status": previous_status,
                "status": "pending",
            },
        }
        await self._audit(
            actor_user_id=actor.id,
            action="tool_accounts.runtime_migration_start",
            target_type="tool_account",
            target_id=str(account.id),
            details={"source": source_backend, "target": target_backend, "task_id": task_id},
        )
        await self._session.commit()
        return RuntimeMigrationData(
            tool_account_id=account.id,
            source_runtime_backend=source_backend,
            target_runtime_backend=target_backend,
            status="pending",
            task_id=task_id,
        )

    async def plan_config_import(
        self,
        *,
        user: User,
        account_id: UUID,
        tool_type: str,
        include: list[str],
        exclude: list[str],
        files: list[ToolAccountConfigImportFile],
        include_resume_history: bool,
        dry_run: bool,
    ) -> ToolAccountConfigImportData:
        """
        生成工具账户配置导入计划
        """

        account = await self.get_account(user=user, account_id=account_id)
        if account.tool_type != tool_type:
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Tool type does not match tool account.",
                status_code=422,
            )
        excluded = set(exclude)
        accepted, rejected, warnings = self._classify_config_import_paths(
            include=include,
            exclude=excluded,
            include_resume_history=include_resume_history,
        )
        task_id: str | None = None
        account_remote_path: str | None = None
        imported_file_count: int | None = None
        if not dry_run:
            if not files:
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message="Config import files are required when dry_run=false.",
                    status_code=422,
                )
            profile = await self._ensure_profile(
                account=account,
                template=self._require_template(account.tool_type),
            )
            node = await self._choose_binding_node(account)
            if account.runtime_backend is None:
                account.runtime_backend = node.default_runtime_backend
            account.affinity_node_id = node.id
            account_remote_path = self._profile_text(
                profile.profile_json,
                "account_remote_path",
                self._account_remote_path(user.id, account.tool_type, account.id),
            )
            accepted_files = self._validate_config_import_files(
                files=files,
                accepted_roots=accepted,
                include_resume_history=include_resume_history,
            )
            imported_file_count = len(accepted_files)
            task_id = f"import_tool_account_config:{account.id}:{uuid4()}"
            await self._repository.add_task(
                NodeTask(
                    node_id=node.id,
                    task_id=task_id,
                    task_type="import_tool_account_config",
                    status="pending",
                    payload={
                        "tool_account_id": str(account.id),
                        "tool_type": account.tool_type,
                        "user_id": str(user.id),
                        "account_remote_path": account_remote_path,
                        "runtime_backend": account.runtime_backend,
                        "files": accepted_files,
                    },
                    retry_count=0,
                )
            )
            profile.profile_json = {
                **profile.profile_json,
                "account_remote_path": account_remote_path,
                "last_config_import_task_id": task_id,
            }
        await self._audit(
            actor_user_id=user.id,
            action=(
                "tool_accounts.config_import_plan"
                if dry_run
                else "tool_accounts.config_import_start"
            ),
            target_type="tool_account",
            target_id=str(account.id),
            details={
                "accepted_count": len(accepted),
                "rejected_count": len(rejected),
                "file_count": imported_file_count or 0,
                "include_resume_history": include_resume_history,
                "dry_run": dry_run,
                "task_id": task_id,
            },
        )
        await self._session.commit()
        return ToolAccountConfigImportData(
            tool_account_id=account.id,
            accepted=accepted,
            rejected=rejected,
            warnings=warnings,
            task_id=task_id,
            account_remote_path=account_remote_path,
            imported_file_count=imported_file_count,
            dry_run=dry_run,
        )

    def _classify_config_import_paths(
        self,
        *,
        include: list[str],
        exclude: set[str],
        include_resume_history: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        accepted: list[str] = []
        rejected: list[str] = []
        warnings: list[str] = []
        for path in include:
            normalized = self._normalize_config_path(path)
            if normalized in exclude:
                rejected.append(path)
                continue
            if normalized in DENIED_CONFIG_IMPORT_PATHS:
                rejected.append(path)
                continue
            if normalized in RESUME_HISTORY_PATHS and not include_resume_history:
                rejected.append(path)
                warnings.append("Resume history requires explicit include_resume_history=true.")
                continue
            if normalized in DEFAULT_CONFIG_IMPORT_PATHS or normalized in RESUME_HISTORY_PATHS:
                accepted.append(path)
                continue
            if any(normalized.startswith(prefix) for prefix in ASK_CONFIG_IMPORT_PREFIXES):
                accepted.append(path)
                warnings.append(f"{path} may contain executable code and requires user review.")
                continue
            if normalized.endswith(".md") and normalized.startswith("~/.claude/"):
                accepted.append(path)
                warnings.append(f"{path} is a custom Markdown file and requires user review.")
                continue
            rejected.append(path)
        return accepted, rejected, warnings

    def _validate_config_import_files(
        self,
        *,
        files: list[ToolAccountConfigImportFile],
        accepted_roots: list[str],
        include_resume_history: bool,
    ) -> list[dict[str, object]]:
        accepted_files: list[dict[str, object]] = []
        total_size = 0
        for file in files:
            path = self._normalize_config_path(file.path)
            if not self._config_file_allowed(path, accepted_roots):
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message=f"Config file path is not allowed: {file.path}",
                    status_code=422,
                )
            if not include_resume_history and any(
                path == root or path.startswith(f"{root}/") for root in RESUME_HISTORY_PATHS
            ):
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message="Resume history requires explicit include_resume_history=true.",
                    status_code=422,
                )
            try:
                content = base64.b64decode(file.content_base64, validate=True)
            except binascii.Error as error:
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message=f"Config file content is not valid base64: {file.path}",
                    status_code=422,
                ) from error
            if len(content) > CONFIG_IMPORT_MAX_FILE_BYTES:
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message=f"Config file is too large: {file.path}",
                    status_code=422,
                )
            total_size += len(content)
            if total_size > CONFIG_IMPORT_MAX_TOTAL_BYTES:
                raise ApiError(
                    code="COMMON_VALIDATION_ERROR",
                    message="Config import payload is too large.",
                    status_code=422,
                )
            accepted_files.append(
                {
                    "path": path,
                    "content_base64": file.content_base64,
                    "mode": file.mode,
                }
            )
        return accepted_files

    def _config_file_allowed(self, path: str, accepted_roots: list[str]) -> bool:
        if not self._is_safe_config_file_path(path):
            return False
        if path in DENIED_CONFIG_IMPORT_PATHS:
            return False
        return any(path == root or path.startswith(f"{root}/") for root in accepted_roots)

    def _is_safe_config_file_path(self, path: str) -> bool:
        if not path.startswith("~/.claude/"):
            return False
        if "\\" in path or "\x00" in path:
            return False
        parts = path.removeprefix("~/.claude/").split("/")
        return all(part not in {"", ".", ".."} for part in parts)

    async def start_binding(
        self, *, user: User, token: AuthToken, account_id: UUID
    ) -> BindingSession:
        """
        创建工具账户绑定任务

        :param user (User): 当前用户
        :param account_id (UUID): 工具账户 ID

        :return BindingSession: 绑定会话
        """

        account = await self._require_account(user=user, account_id=account_id)
        template = self._require_template(account.tool_type)
        node = await self._choose_binding_node(account)
        if account.runtime_backend is None:
            account.runtime_backend = node.default_runtime_backend
        if account.runtime_backend == "native" and token.user_device_id is None:
            raise ApiError(
                code="DEVICE_REQUIRED",
                message="Native account binding requires a device token.",
                status_code=403,
            )
        profile = await self._ensure_profile(account=account, template=template)

        account_remote_path = self._profile_text(
            profile.profile_json,
            "account_remote_path",
            self._account_remote_path(user.id, account.tool_type, account.id),
        )
        attempt_id = uuid4().hex[:12]
        binding_session_id = f"bind-{account.id.hex[:12]}-{attempt_id}"
        tmux_session_name = f"{self._tmux_session_name(account)}-{attempt_id}"
        task_id = f"create_binding_session:{account.id}:{attempt_id}"

        account.affinity_node_id = node.id
        account.status = "binding_session_starting"
        profile.profile_json = {
            **profile.profile_json,
            "binding_session_id": binding_session_id,
            "binding_task_id": task_id,
            "tmux_session_name": tmux_session_name,
            "account_remote_path": account_remote_path,
            "template": self._template_payload(template),
            "verifier": template.verifier,
            "local_cli_secrets": False,
            "last_error": None,
        }
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
                    "runtime_backend": account.runtime_backend,
                    "runtime_policy": node.runtime_policy,
                    "tmux_session_name": tmux_session_name,
                    "template": self._template_payload(template),
                    "verifier": template.verifier,
                },
                retry_count=0,
            )
        )
        if account.runtime_backend == "native":
            assert token.user_device_id is not None
            device = await self._identity_repository.get_device(token.user_device_id)
            if device is None or device.user_id != user.id or device.status != "active":
                raise ApiError(
                    code="DEVICE_REVOKED", message="Device is not active.", status_code=403
                )
            ssh_keys = [
                key
                for key in await self._identity_repository.list_ssh_keys_for_device(device.id)
                if key.status == "active"
            ]
            if not ssh_keys:
                raise ApiError(
                    code="SSH_KEY_MISSING",
                    message="Current device has no active SSH key.",
                    status_code=409,
                )
            ssh_task_id = f"sync_ssh_keys:{node.id}:{device.id}:{ssh_keys[0].id}"
            if await self._repository.get_task_by_task_id(ssh_task_id) is None:
                await self._repository.add_task(
                    NodeTask(
                        node_id=node.id,
                        task_id=ssh_task_id,
                        task_type="sync_ssh_keys",
                        status="pending",
                        payload={
                            "device_id": str(device.id),
                            "ssh_user": node.ssh_user or "agent-remote",
                            "authorized_keys_path": None,
                            "ssh_keys": [
                                {
                                    "id": str(key.id),
                                    "public_key": key.public_key,
                                    "forced_command": f"agent-remote-attach --device {device.id}",
                                }
                                for key in ssh_keys
                            ],
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
        task_id = (
            self._profile_text(profile.profile_json, "binding_task_id", "")
            if profile is not None
            else ""
        )
        task = await self._repository.get_task_by_task_id(task_id) if task_id else None
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
            self._account_remote_path(user.id, account.tool_type, account.id),
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
        node = next(
            (candidate for candidate in candidates if self._node_can_host(candidate, account)), None
        )
        if node is None:
            account.status = "node_unavailable"
            await self._session.flush()
            raise ApiError(
                code="NODE_UNAVAILABLE",
                message="No available node can host this tool account.",
                status_code=409,
            )
        return node

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
                    "account_remote_path": self._account_remote_path(
                        account.user_id, account.tool_type, account.id
                    ),
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
            connect_command=self._connect_command(node, profile_json, account),
            task_id=task.task_id if task is not None else None,
            verifier=self._optional_text(profile_json, "verifier"),
            error=self._optional_text(profile_json, "last_error"),
            runtime_backend=account.runtime_backend,
        )

    def _connect_command(
        self, node: Node | None, profile_json: dict[str, object], account: ToolAccount
    ) -> str | None:
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
        if account.runtime_backend == "native":
            return (
                f"ssh -p {ssh_port} {ssh_user}@{ssh_host} "
                f"agent-remote-attach --binding {account.id}"
            )
        return f"ssh -p {ssh_port} {ssh_user}@{ssh_host} tmux attach-session -t {tmux_session_name}"

    def _node_can_host(self, node: Node | None, account: ToolAccount) -> bool:
        if node is None:
            return False
        if node.status not in ACTIVE_NODE_STATUSES:
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

    def _account_remote_path(self, user_id: UUID, tool_type: str, account_id: UUID) -> str:
        return f"{ACCOUNT_CONFIG_ROOT}/{user_id}/tool-accounts/{tool_type}/{account_id}"

    def _profile_text(self, profile_json: dict[str, object], key: str, default: str) -> str:
        value = profile_json.get(key)
        if isinstance(value, str) and value:
            return value
        return default

    def _normalize_config_path(self, path: str) -> str:
        return path.rstrip("/")

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
