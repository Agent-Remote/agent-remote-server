"""
实现会话业务逻辑。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.ego_browser.relay import EgoBrowserRevocationPublisher
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    DeveloperCredentialProfile,
    Node,
    NodeTask,
    Session,
    ToolAccount,
    User,
    Workspace,
)
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.sessions import SessionRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.device_sessions import (
    DeviceSessionService,
    RevokedDeviceBinding,
)
from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.port_forward_revocation import revoke_port_forwards
from agent_remote_server.services.runtime_migrations import require_runtime_migration_settled
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.lifecycle import existing_history_mutations
from agent_remote_server.services.skills.runtime_capability import supports_managed_skills
from agent_remote_server.services.skills.session_admission import (
    SkillAdmissionPending,
    SkillSessionAdmission,
)
from agent_remote_server.services.skills.session_retention import release_retained_session_reference
from agent_remote_server.services.skills.takeover_admission import SkillTakeoverPending
from agent_remote_server.services.tool_accounts import ACCOUNT_CONFIG_ROOT, ACTIVE_NODE_STATUSES
from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate

ACTIVE_SESSION_STATUSES = {"starting", "running", "active"}
DELETABLE_SESSION_STATUSES = {"stopped", "interrupted", "failed"}
SESSION_STATUSES = {
    "starting",
    "running",
    "active",
    "stopping",
    "stopped",
    "interrupted",
    "failed",
}


class ToolSessionService:
    """
    工具运行 session 生命周期服务
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_hub: DeviceRelayHub | None = None,
        ego_browser_revocation_publisher: EgoBrowserRevocationPublisher | None = None,
    ) -> None:
        """
        初始化工具会话业务服务。

        :param session (AsyncSession): 会话
        :param settings (Settings): 配置
        :param relay_hub (DeviceRelayHub | None): 中继中心
        :param ego_browser_revocation_publisher (EgoBrowserRevocationPublisher | None): 撤销发布器
        """
        self._session = session
        self._settings = settings
        self._repository = SessionRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._registry = ToolRegistry()
        self._relay_hub = relay_hub
        self._ego_browser_revocation_publisher = ego_browser_revocation_publisher

    async def list_sessions(
        self, *, user: User, tool_type: str | None, statuses: list[str] | None
    ) -> list[tuple[Session, Workspace]]:
        """
        列出用户工具 session

        :param user (User): 当前用户
        :param tool_type (str | None): 工具类型
        :param statuses (list[str] | None): 工具会话状态过滤
        :return list[tuple[Session, Workspace]]: 工具 session 与 workspace 列表
        :raises ApiError: 状态过滤值不受支持
        """

        normalized_statuses = sorted(set(statuses or []))
        invalid_statuses = set(normalized_statuses) - SESSION_STATUSES
        if invalid_statuses:
            raise ApiError(
                code="SESSION_STATUS_INVALID",
                message="Session status filter is invalid.",
                status_code=422,
            )
        return list(
            await self._repository.list_sessions_for_user(
                user.id, tool_type, normalized_statuses or None
            )
        )

    async def get_current_project_session(
        self, *, user: User, tool_type: str, project_key: str
    ) -> Session:
        """
        读取当前项目最近可恢复工具 session

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :param project_key (str): 项目 key
        :return Session: 工具 session 实体
        :raises ApiError: 当前项目没有可恢复的工具 session
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
        :param session_id (UUID): 工具会话 ID
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
        启动异常回滚全部保存点，正常迁移冲突提交后明确拒绝创建会话。

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :param tool_account_id (UUID): 已授权账户
        :param workspace_id (UUID): 已授权工作区
        :param project_key (str): 项目身份
        :param argv (list[str]): 工具参数
        :param replaces_session_id (UUID | None): 可选替代会话
        :return Session: 完整受理的启动会话
        """
        try:
            async with self._session.begin_nested():
                result = await self._create_session(
                    user=user,
                    tool_type=tool_type,
                    tool_account_id=tool_account_id,
                    workspace_id=workspace_id,
                    project_key=project_key,
                    argv=argv,
                    replaces_session_id=replaces_session_id,
                )
        except SkillContentError as error:
            status = 413 if error.code in {"QUOTA_EXCEEDED", "CONTENT_TOO_LARGE"} else 409
            raise ApiError(
                code=error.code, message=str(error), status_code=status, details=error.details
            ) from error
        await self._session.commit()
        if isinstance(result, SkillTakeoverPending):
            raise ApiError(
                code="MIGRATION_PENDING",
                message="Account skill takeover is pending; existing sessions may finish normally.",
                status_code=409,
                details={
                    "account_id": str(result.account_id),
                    "takeover_id": str(result.takeover_id),
                    "takeover_status": result.status,
                    "reservation_committed": True,
                    "session_created": False,
                },
            )
        if isinstance(result, SkillAdmissionPending):
            raise ApiError(
                code="STATE_MIGRATION_REQUIRED",
                message="Resolve the retained skill migrations before starting this account.",
                status_code=409,
                details={
                    "account_id": str(result.account_id),
                    "migration_ids": [str(identity) for identity in result.migration_ids],
                    "preparations_committed": True,
                },
            )
        return result

    async def _create_session(
        self,
        *,
        user: User,
        tool_type: str,
        tool_account_id: UUID,
        workspace_id: UUID,
        project_key: str,
        argv: list[str],
        replaces_session_id: UUID | None = None,
    ) -> Session | SkillAdmissionPending | SkillTakeoverPending:
        """
        创建工具运行 session 并投递节点任务

        :param user (User): 当前用户
        :param tool_type (str): 工具类型
        :param tool_account_id (UUID): 工具账户 ID
        :param workspace_id (UUID): 工作区 ID
        :param project_key (str): 项目 key
        :param argv (list[str]): 工具 CLI 透传参数
        :param replaces_session_id (UUID | None): 被替代的中断会话 ID
        :return Session | SkillAdmissionPending | SkillTakeoverPending: 工具会话或需保留的迁移冲突
        :raises ApiError: 工具账户、工作区、替代会话或可用节点不满足创建条件
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
        admission = SkillSessionAdmission(self._session, self._settings)
        managed = await admission.required(account)
        await self._require_active_account(
            user=user, tool_type=template.tool_type, account_id=account.id
        )
        node = await self._choose_session_node(account, managed=managed)
        if managed:
            pending = await admission.prepare(account, node)
            if pending is not None:
                latest_node = await self._repository.get_node(node.id)
                if not self._node_can_host(latest_node, account, managed=True):
                    raise ApiError(
                        code="SKILL_MANAGER_UNSUPPORTED",
                        message="Runtime skill capability changed during preparation.",
                        status_code=409,
                    )
                return pending
        if account.runtime_backend is None:
            account.runtime_backend = node.default_runtime_backend
        runtime_backend = account.runtime_backend
        device_control_protocol_version = self._device_control_protocol_version(
            node,
            tool_type=template.tool_type,
            runtime_backend=runtime_backend,
        )
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
                device_control_protocol_version=device_control_protocol_version,
                replaces_session_id=replaced_session.id if replaced_session is not None else None,
            )
        )
        tmux_session_name = self._tmux_session_name(tool_session)
        sandbox_name = self._sandbox_name(tool_session)
        tool_session.tmux_session_name = tmux_session_name
        tool_session.container_id = sandbox_name
        account.affinity_node_id = node.id
        task_id = f"create_tool_session:{tool_session.id}"
        task = await self._repository.add_task(
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
                    "device_control": (
                        {"protocol_version": device_control_protocol_version}
                        if device_control_protocol_version is not None
                        else None
                    ),
                },
                retry_count=0,
            )
        )
        if managed:
            latest_node = await self._repository.get_node(node.id)
            if not self._node_can_host(latest_node, account, managed=True):
                raise ApiError(
                    code="SKILL_MANAGER_UNSUPPORTED",
                    message="Runtime skill capability changed during preparation.",
                    status_code=409,
                )
            await admission.reserve(tool_session, task, node)
        await self._audit(
            actor_user_id=user.id,
            action="sessions.create",
            target_type="session",
            target_id=str(tool_session.id),
            details={"node_id": str(node.id), "task_id": task_id},
        )
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
            ego_service = EgoBrowserService(
                self._session,
                self._settings,
                revocation_publisher=self._ego_browser_revocation_publisher,
            )
            await ego_service.revoke_for_tool_session(
                tool_session_id=tool_session.id,
                reason="tool_session_stop",
                commit=False,
                publish=False,
            )
            await self._session.commit()
            await ego_service.publish_pending_revocations()
            return tool_session
        async with existing_history_mutations(self._session, (user.id,)):
            device_stop = await DeviceSessionService(
                self._session, self._settings, self._relay_hub
            ).stop_for_tool_session(
                tool_session_id=tool_session.id,
                reason="tool_session_stop",
                actor_user_id=user.id,
                audit_action="device_session.session_stop",
                commit=False,
            )
            ego_service = EgoBrowserService(
                self._session,
                self._settings,
                revocation_publisher=self._ego_browser_revocation_publisher,
            )
            await ego_service.revoke_for_tool_session(
                tool_session_id=tool_session.id,
                reason="tool_session_stop",
                commit=False,
                publish=False,
            )
            task_id = f"stop_tool_session:{tool_session.id}"
            existing = await self._repository.get_task_by_task_id(task_id)
            if existing is None:
                snapshot = await SkillRuntimeRepository(self._session).snapshot_for_session(
                    user.id, tool_session.id
                )
                finalization_pointer = (
                    {
                        "skill_finalization": {
                            "snapshot_id": str(snapshot.id),
                            "task_id": str(snapshot.prepare_task_id),
                            "user_id": str(snapshot.user_id),
                            "account_id": str(snapshot.account_id),
                        }
                    }
                    if snapshot is not None
                    else {}
                )
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
                            **finalization_pointer,
                        },
                        retry_count=0,
                    )
                )
            tool_session.status = "stopping"
            await revoke_port_forwards(
                self._session,
                reason="session_not_running",
                actor_user_id=user.id,
                session_id=tool_session.id,
            )
            await self._audit(
                actor_user_id=user.id,
                action="sessions.stop",
                target_type="session",
                target_id=str(tool_session.id),
                details={"task_id": task_id},
            )
        await self._session.commit()
        await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).close_revoked_bindings(device_stop.revoked_bindings)
        await ego_service.publish_pending_revocations()
        return tool_session

    async def delete_session(self, *, user: User, session_id: UUID) -> None:
        """
        删除当前用户已停止、已中断或失败的工具 session

        :param user (User): 当前用户
        :param session_id (UUID): 工具 session ID
        :raises ApiError: session 不属于当前用户或尚未进入可删除状态
        """

        tool_session = await self._require_user_session(user=user, session_id=session_id)
        if tool_session.status not in DELETABLE_SESSION_STATUSES:
            raise ApiError(
                code="SESSION_DELETE_NOT_ALLOWED",
                message="Only stopped, interrupted, or failed sessions can be deleted.",
                status_code=409,
            )
        await release_retained_session_reference(self._session, tool_session)
        device_stop = await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).stop_for_tool_session(
            tool_session_id=tool_session.id,
            reason="tool_session_delete",
            actor_user_id=user.id,
            audit_action="device_session.session_stop",
            commit=False,
        )
        ego_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        await ego_service.revoke_for_tool_session(
            tool_session_id=tool_session.id,
            reason="tool_session_delete",
            commit=False,
            publish=False,
        )
        await revoke_port_forwards(
            self._session,
            reason="session_deleted",
            actor_user_id=user.id,
            session_id=tool_session.id,
        )
        await self._audit(
            actor_user_id=user.id,
            action="sessions.delete",
            target_type="session",
            target_id=str(tool_session.id),
            details={"status": tool_session.status},
        )
        await self._repository.delete_session(tool_session)
        await self._session.commit()
        await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).close_revoked_bindings(device_stop.revoked_bindings)
        await ego_service.publish_pending_revocations()

    async def delete_inactive_sessions(self, *, user: User) -> int:
        """
        删除当前用户全部已停止、已中断和失败的工具 session

        :param user (User): 当前用户
        :return int: 删除数量
        """

        sessions = list(
            await self._repository.list_sessions_for_user_by_statuses(
                user.id, DELETABLE_SESSION_STATUSES
            )
        )
        if not sessions:
            return 0
        for tool_session in sessions:
            await release_retained_session_reference(self._session, tool_session)
        revoked_bindings: list[RevokedDeviceBinding] = []
        ego_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        for tool_session in sessions:
            device_stop = await DeviceSessionService(
                self._session, self._settings, self._relay_hub
            ).stop_for_tool_session(
                tool_session_id=tool_session.id,
                reason="tool_session_bulk_delete",
                actor_user_id=user.id,
                audit_action="device_session.session_stop",
                commit=False,
            )
            revoked_bindings.extend(device_stop.revoked_bindings)
            await ego_service.revoke_for_tool_session(
                tool_session_id=tool_session.id,
                reason="tool_session_bulk_delete",
                commit=False,
                publish=False,
            )
            await revoke_port_forwards(
                self._session,
                reason="session_deleted",
                actor_user_id=user.id,
                session_id=tool_session.id,
            )
            await self._repository.delete_session(tool_session)
        await self._audit(
            actor_user_id=user.id,
            action="sessions.bulk_delete",
            target_type="session",
            target_id=str(user.id),
            details={
                "deleted_count": len(sessions),
                "statuses": sorted(DELETABLE_SESSION_STATUSES),
            },
        )
        await self._session.commit()
        await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).close_revoked_bindings(revoked_bindings)
        await ego_service.publish_pending_revocations()
        return len(sessions)

    async def _require_user_session(self, *, user: User, session_id: UUID) -> Session:
        """
        获取并校验用户会话。

        :param user (User): 用户
        :param session_id (UUID): 会话 ID
        :return Session: 用户会话
        """
        tool_session = await self._repository.get_session(session_id)
        if tool_session is None or tool_session.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        return tool_session

    async def _require_active_account(
        self, *, user: User, tool_type: str, account_id: UUID
    ) -> ToolAccount:
        """
        获取并校验活动状态账号。

        :param user (User): 用户
        :param tool_type (str): 工具类型
        :param account_id (UUID): 账号 ID
        :return ToolAccount: 活动状态账号
        """
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
        await require_runtime_migration_settled(self._session, user.id, account.id)
        if account.status != "active":
            raise ApiError(
                code="TOOL_ACCOUNT_NOT_ACTIVE",
                message="Tool account is not active.",
                status_code=409,
            )
        return account

    async def _choose_session_node(self, account: ToolAccount, *, managed: bool = False) -> Node:
        """
        选择会话节点。

        :param account (ToolAccount): 账号
        :param managed (bool): 是否必须具有完整技能能力
        :return Node: 会话节点
        """
        active_sessions = await self._repository.list_active_sessions_for_account(account.id)
        if active_sessions:
            node = await self._repository.get_node(active_sessions[0].node_id)
            if node is not None and self._node_can_host(node, account, managed=managed):
                return node
            raise ApiError(
                code="SKILL_MANAGER_UNSUPPORTED" if managed else "NODE_UNAVAILABLE",
                message="Active sessions for this account are pinned to an unavailable node.",
                status_code=409,
            )
        if account.affinity_node_id is not None:
            node = await self._repository.get_node(account.affinity_node_id)
            if node is not None and self._node_can_host(node, account, managed=managed):
                return node
        candidates = await self._repository.list_candidate_nodes(
            tool_type=account.tool_type,
            region_code=account.region_code,
            preferred_tags=account.preferred_node_tags,
        )
        node = next(
            (
                candidate
                for candidate in candidates
                if self._node_can_host(candidate, account, managed=managed)
            ),
            None,
        )
        if node is None:
            raise ApiError(
                code="SKILL_MANAGER_UNSUPPORTED" if managed else "NODE_UNAVAILABLE",
                message="No available node can host this tool session.",
                status_code=409,
            )
        return node

    def _node_can_host(
        self, node: Node | None, account: ToolAccount, *, managed: bool = False
    ) -> bool:
        """
        判断节点能否承载浏览器会话。

        :param node (Node | None): 节点
        :param account (ToolAccount): 账号
        :param managed (bool): 是否要求当前后端完整技能支持
        :return bool: 是否满足校验条件
        """
        if node is None or node.status not in ACTIVE_NODE_STATUSES:
            return False
        if account.tool_type not in node.supported_tool_types:
            return False
        if node.region_code != account.region_code:
            return False
        backend = account.runtime_backend or node.default_runtime_backend
        if backend not in node.allowed_runtime_backends:
            return False
        if managed and not supports_managed_skills(node, backend, self._settings):
            return False
        available = node.runtime_capabilities.get("backends")
        if isinstance(available, list):
            return backend in available
        return not managed and backend == "docker_sandbox"

    def _runtime_payload(self, template: ToolRuntimeTemplate, argv: list[str]) -> dict[str, object]:
        """
        返回运行时载荷。

        :param template (ToolRuntimeTemplate): 模板
        :param argv (list[str]): 命令行参数
        :return dict[str, object]: 运行时载荷
        """
        command = [template.sandbox_agent, *argv]
        return {
            "sandbox_agent": template.sandbox_agent,
            "command": command,
            "verifier": template.verifier,
        }

    def _device_control_protocol_version(
        self,
        node: Node,
        *,
        tool_type: str,
        runtime_backend: str,
    ) -> int | None:
        """
        返回设备控制协议版本。

        :param node (Node): 节点
        :param tool_type (str): 工具类型
        :param runtime_backend (str): 运行时后端
        :return int | None: 设备控制协议版本
        """
        if not self._settings.device_control_enabled or tool_type != "claude":
            return None
        capability = node.runtime_capabilities.get("device_control")
        if not isinstance(capability, dict) or capability.get("supported") is not True:
            return None
        protocols = capability.get("protocol_versions")
        platforms = capability.get("platforms")
        backends = capability.get("backends")
        if (
            isinstance(protocols, list)
            and 1 in protocols
            and isinstance(platforms, list)
            and "macos" in platforms
            and isinstance(backends, list)
            and runtime_backend in backends
        ):
            return 1
        return None

    def _tmux_session_name(self, tool_session: Session) -> str:
        """
        返回tmux 会话名称。

        :param tool_session (Session): 工具会话
        :return str: tmux 会话名称
        """
        return f"ar-{tool_session.tool_type}-{str(tool_session.id).replace('-', '')[:24]}"

    def _sandbox_name(self, tool_session: Session) -> str:
        """
        返回沙箱名称。

        :param tool_session (Session): 工具会话
        :return str: 沙箱名称
        """
        return f"agent-remote-{tool_session.tool_type}-{str(tool_session.id).replace('-', '')[:24]}"

    def _account_remote_path(self, user_id: UUID, tool_type: str, account_id: UUID) -> str:
        """
        返回账号远端路径。

        :param user_id (UUID): 用户 ID
        :param tool_type (str): 工具类型
        :param account_id (UUID): 账号 ID
        :return str: 账号远端路径
        """
        return f"{ACCOUNT_CONFIG_ROOT}/{user_id}/tool-accounts/{tool_type}/{account_id}"

    def _developer_credential_profile_path(self, user_id: UUID, profile_id: UUID) -> str:
        """
        返回开发者凭据配置路径。

        :param user_id (UUID): 用户 ID
        :param profile_id (UUID): 配置 ID
        :return str: 开发者凭据配置路径
        """
        return f"{ACCOUNT_CONFIG_ROOT}/{user_id}/developer-credential-profiles/{profile_id}"

    def _developer_credentials_payload(
        self, profile: DeveloperCredentialProfile | None
    ) -> dict[str, object] | None:
        """
        返回开发者凭据载荷。

        :param profile (DeveloperCredentialProfile | None): 配置
        :return dict[str, object] | None: 开发者凭据载荷
        """
        if profile is None:
            return None
        return {
            "profile_id": str(profile.id),
            "git_identity": profile.git_identity,
            "gh_mode": profile.github_cli_mode,
            "ssh_mode": profile.ssh_mode,
        }

    def _profile_text(self, profile_json: dict[str, object], key: str, default: str) -> str:
        """
        返回配置文本。

        :param profile_json (dict[str, object]): 配置 json
        :param key (str): 键
        :param default (str): 默认值
        :return str: 配置文本
        """
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
        """
        读取审计记录。

        :param actor_user_id (UUID | None): actor 用户 ID
        :param action (str): 操作
        :param target_type (str): target 类型
        :param target_id (str): 审计目标 ID
        :param details (dict[str, object]): 详情
        """
        await self._identity_repository.add_audit_log(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )
