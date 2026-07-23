from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    Node,
    NodeTask,
    SyncSession,
    User,
    Workspace,
)
from agent_remote_server.repositories.connections import ConnectionRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.workspaces import WorkspaceRepository


@dataclass(frozen=True)
class SyncSessionResult:
    """
    同步 session 创建结果
    """

    sync_session: SyncSession
    node: Node | None
    prepare_task_id: str | None


class WorkspaceService:
    """
    workspace 和同步 session 服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = WorkspaceRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._connection_repository = ConnectionRepository(session)

    async def list_workspaces(self, *, user: User) -> list[Workspace]:
        """
        列出用户 workspace

        :param user (User): 当前用户

        :return list: workspace 列表
        """

        return list(await self._repository.list_workspaces_for_user(user.id))

    async def create_workspace(
        self,
        *,
        user: User,
        token: AuthToken,
        device_id: UUID,
        project_key: str,
        local_start_path: str,
        display_name: str,
        sync_git: bool,
        git_sync_policy: dict[str, object],
    ) -> Workspace:
        """
        创建或复用 workspace

        :param user (User): 当前用户
        :param token (AuthToken): 当前令牌
        :param device_id (UUID): 设备 ID
        :param project_key (str): 项目 key
        :param local_start_path (str): 本地启动路径
        :param display_name (str): 显示名称
        :param sync_git (bool): 是否同步 .git 目录
        :param git_sync_policy (dict): Git 同步策略

        :return Workspace: workspace 实体
        """

        self._require_token_device(token=token, device_id=device_id)
        device = await self._repository.get_active_device(user_id=user.id, device_id=device_id)
        if device is None:
            raise ApiError(code="DEVICE_REVOKED", message="Device is not active.", status_code=403)

        existing = await self._repository.get_workspace_by_project_key(
            user_id=user.id, project_key=project_key
        )
        if existing is not None:
            return existing

        workspace = await self._repository.add_workspace(
            Workspace(
                user_id=user.id,
                device_id=device.id,
                project_key=project_key,
                local_start_path=local_start_path,
                display_name=display_name,
                remote_path=None,
                sync_git=sync_git,
                git_sync_policy=git_sync_policy,
            )
        )
        workspace.remote_path = self._remote_workspace_path(
            user_id=user.id, workspace_id=workspace.id
        )
        await self._audit(
            actor_user_id=user.id,
            action="workspaces.create",
            target_type="workspace",
            target_id=str(workspace.id),
            details={"project_key": project_key, "device_id": str(device.id)},
        )
        await self._session.commit()
        return workspace

    async def get_workspace(self, *, user: User, workspace_id: UUID) -> Workspace:
        """
        读取用户 workspace

        :param user (User): 当前用户
        :param workspace_id (UUID): workspace ID

        :return Workspace: workspace 实体
        """

        return await self._require_workspace(user=user, workspace_id=workspace_id)

    async def update_workspace(
        self,
        *,
        user: User,
        workspace_id: UUID,
        local_start_path: str | None,
        display_name: str | None,
        sync_git: bool | None,
        git_sync_policy: dict[str, object] | None,
    ) -> Workspace:
        """
        更新 workspace 元数据

        :param user (User): 当前用户
        :param workspace_id (UUID): workspace ID
        :param local_start_path (str): 本地启动路径
        :param display_name (str): 显示名称
        :param sync_git (bool): 是否同步 .git 目录
        :param git_sync_policy (dict): Git 同步策略

        :return Workspace: workspace 实体
        """

        workspace = await self._require_workspace(user=user, workspace_id=workspace_id)
        if local_start_path is not None:
            workspace.local_start_path = local_start_path
        if display_name is not None:
            workspace.display_name = display_name
        if sync_git is not None:
            workspace.sync_git = sync_git
        if git_sync_policy is not None:
            workspace.git_sync_policy = git_sync_policy
        await self._audit(
            actor_user_id=user.id,
            action="workspaces.update",
            target_type="workspace",
            target_id=str(workspace.id),
            details={},
        )
        await self._session.commit()
        return workspace

    async def list_sync_sessions(self, *, user: User) -> list[SyncSessionResult]:
        """
        列出用户同步 session

        :param user (User): 当前用户

        :return list: 同步 session 结果
        """

        items = list(await self._repository.list_sync_sessions_for_user(user.id))
        results: list[SyncSessionResult] = []
        for item in items:
            node = await self._repository.get_node(item.node_id) if item.node_id else None
            results.append(SyncSessionResult(sync_session=item, node=node, prepare_task_id=None))
        return results

    async def create_sync_session(
        self,
        *,
        user: User,
        workspace_id: UUID,
        node_id: UUID | None,
        local_path: str | None,
        sync_mode: str,
        sync_git: bool,
        exclude: list[str],
    ) -> SyncSessionResult:
        """
        创建或复用同步 session

        :param user (User): 当前用户
        :param workspace_id (UUID): workspace ID
        :param node_id (UUID): 节点 ID
        :param local_path (str): 本地路径
        :param sync_mode (str): 同步模式
        :param sync_git (bool): 是否同步 .git 目录
        :param exclude (list): 排除规则

        :return SyncSessionResult: 同步 session 结果
        """

        if sync_mode != "two_way":
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Only two_way sync mode is supported.",
                status_code=422,
            )
        workspace = await self._require_workspace(user=user, workspace_id=workspace_id)
        existing = await self._repository.get_current_sync_session_for_workspace(
            workspace_id=workspace.id
        )
        if existing is not None:
            node = await self._repository.get_node(existing.node_id) if existing.node_id else None
            return SyncSessionResult(sync_session=existing, node=node, prepare_task_id=None)

        node = await self._select_node(node_id)
        if workspace.remote_path is None:
            workspace.remote_path = self._remote_workspace_path(
                user_id=user.id, workspace_id=workspace.id
            )
        sync_session = await self._repository.add_sync_session(
            SyncSession(
                user_id=user.id,
                workspace_id=workspace.id,
                node_id=node.id,
                local_path=local_path or workspace.local_start_path,
                remote_path=workspace.remote_path,
                status="starting",
                conflict_status="none",
                sync_mode="two_way",
                sync_git=sync_git,
                exclude_patterns=exclude,
                mutagen_session_id=None,
            )
        )
        sync_session.mutagen_session_id = self._mutagen_session_name(
            user_id=user.id,
            workspace_id=workspace.id,
            sync_session_id=sync_session.id,
            node_id=node.id,
        )
        prepare_task_id = await self._ensure_prepare_workspace_task(
            node=node, workspace=workspace, sync_session=sync_session
        )
        await self._audit(
            actor_user_id=user.id,
            action="sync_sessions.create",
            target_type="sync_session",
            target_id=str(sync_session.id),
            details={"workspace_id": str(workspace.id), "node_id": str(node.id)},
        )
        await self._session.commit()
        return SyncSessionResult(
            sync_session=sync_session,
            node=node,
            prepare_task_id=prepare_task_id,
        )

    async def get_sync_session(self, *, user: User, sync_session_id: UUID) -> SyncSessionResult:
        """
        读取同步 session

        :param user (User): 当前用户
        :param sync_session_id (UUID): 同步 session ID

        :return SyncSessionResult: 同步 session 结果
        """

        sync_session = await self._require_sync_session(user=user, sync_session_id=sync_session_id)
        node = (
            await self._repository.get_node(sync_session.node_id) if sync_session.node_id else None
        )
        return SyncSessionResult(sync_session=sync_session, node=node, prepare_task_id=None)

    async def pause_sync_session(self, *, user: User, sync_session_id: UUID) -> SyncSessionResult:
        """
        暂停同步 session

        :param user (User): 当前用户
        :param sync_session_id (UUID): 同步 session ID

        :return SyncSessionResult: 同步 session 结果
        """

        return await self._transition_sync_session(
            user=user,
            sync_session_id=sync_session_id,
            status="paused",
            conflict_status=None,
            action="sync_sessions.pause",
        )

    async def resume_sync_session(self, *, user: User, sync_session_id: UUID) -> SyncSessionResult:
        """
        恢复同步 session

        :param user (User): 当前用户
        :param sync_session_id (UUID): 同步 session ID

        :return SyncSessionResult: 同步 session 结果
        """

        result = await self._transition_sync_session(
            user=user,
            sync_session_id=sync_session_id,
            status="starting",
            conflict_status=None,
            action="sync_sessions.resume",
        )
        sync_session = result.sync_session
        workspace = await self._require_workspace(user=user, workspace_id=sync_session.workspace_id)
        if result.node is not None:
            task_id = await self._ensure_prepare_workspace_task(
                node=result.node, workspace=workspace, sync_session=sync_session
            )
            await self._session.commit()
            return SyncSessionResult(
                sync_session=sync_session, node=result.node, prepare_task_id=task_id
            )
        return result

    async def resolve_sync_session(self, *, user: User, sync_session_id: UUID) -> SyncSessionResult:
        """
        标记同步冲突已解决

        :param user (User): 当前用户
        :param sync_session_id (UUID): 同步 session ID

        :return SyncSessionResult: 同步 session 结果
        """

        return await self._transition_sync_session(
            user=user,
            sync_session_id=sync_session_id,
            status="healthy",
            conflict_status="none",
            action="sync_sessions.resolve",
        )

    async def reset_sync_session(self, *, user: User, sync_session_id: UUID) -> SyncSessionResult:
        """
        重置同步 session

        :param user (User): 当前用户
        :param sync_session_id (UUID): 同步 session ID

        :return SyncSessionResult: 同步 session 结果
        """

        result = await self._transition_sync_session(
            user=user,
            sync_session_id=sync_session_id,
            status="starting",
            conflict_status="none",
            action="sync_sessions.reset",
        )
        sync_session = result.sync_session
        workspace = await self._require_workspace(user=user, workspace_id=sync_session.workspace_id)
        if result.node is not None:
            task_id = await self._ensure_prepare_workspace_task(
                node=result.node, workspace=workspace, sync_session=sync_session
            )
            await self._session.commit()
            return SyncSessionResult(
                sync_session=sync_session, node=result.node, prepare_task_id=task_id
            )
        return result

    async def _transition_sync_session(
        self,
        *,
        user: User,
        sync_session_id: UUID,
        status: str,
        conflict_status: str | None,
        action: str,
    ) -> SyncSessionResult:
        sync_session = await self._require_sync_session(user=user, sync_session_id=sync_session_id)
        sync_session.status = status
        if conflict_status is not None:
            sync_session.conflict_status = conflict_status
        node = (
            await self._repository.get_node(sync_session.node_id) if sync_session.node_id else None
        )
        await self._audit(
            actor_user_id=user.id,
            action=action,
            target_type="sync_session",
            target_id=str(sync_session.id),
            details={"status": status, "conflict_status": sync_session.conflict_status},
        )
        await self._session.commit()
        return SyncSessionResult(sync_session=sync_session, node=node, prepare_task_id=None)

    async def _select_node(self, node_id: UUID | None) -> Node:
        if node_id is not None:
            node = await self._repository.get_node(node_id)
            if node is None:
                raise ApiError(
                    code="COMMON_NOT_FOUND", message="Node was not found.", status_code=404
                )
            if node.status not in {"healthy", "degraded"}:
                raise ApiError(
                    code="NODE_UNHEALTHY",
                    message="Node is not available for workspace sync.",
                    status_code=409,
                )
            return node

        nodes = list(await self._repository.list_connectable_nodes())
        if not nodes:
            raise ApiError(
                code="NODE_UNHEALTHY",
                message="No node is available for workspace sync.",
                status_code=409,
            )
        return nodes[0]

    async def _require_workspace(self, *, user: User, workspace_id: UUID) -> Workspace:
        workspace = await self._repository.get_workspace(workspace_id)
        if workspace is None or workspace.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Workspace was not found.", status_code=404
            )
        return workspace

    async def _require_sync_session(self, *, user: User, sync_session_id: UUID) -> SyncSession:
        sync_session = await self._repository.get_sync_session(sync_session_id)
        if sync_session is None or sync_session.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Sync session was not found.",
                status_code=404,
            )
        return sync_session

    def _require_token_device(self, *, token: AuthToken, device_id: UUID) -> None:
        if token.user_device_id is None:
            raise ApiError(
                code="DEVICE_REQUIRED",
                message="Device token is required.",
                status_code=403,
            )
        if token.user_device_id != device_id:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Device token does not match workspace device.",
                status_code=403,
            )

    async def _ensure_prepare_workspace_task(
        self,
        *,
        node: Node,
        workspace: Workspace,
        sync_session: SyncSession,
    ) -> str:
        task_id = f"prepare_workspace:{sync_session.id}"
        existing = await self._repository.get_task_by_task_id(task_id)
        ssh_keys = list(
            await self._connection_repository.list_active_ssh_keys_for_device(workspace.device_id)
        )
        if not ssh_keys:
            raise ApiError(
                code="SSH_KEY_MISSING",
                message="Workspace device has no active SSH key.",
                status_code=409,
            )
        payload: dict[str, object] = {
            "user_id": str(workspace.user_id),
            "workspace_id": str(workspace.id),
            "sync_session_id": str(sync_session.id),
            "remote_path": sync_session.remote_path,
            "sync_git": sync_session.sync_git,
            "exclude": sync_session.exclude_patterns,
            "git_sync_policy": workspace.git_sync_policy,
            "device_id": str(workspace.device_id),
            "ssh_keys": [
                {
                    "id": str(ssh_key.id),
                    "public_key": ssh_key.public_key,
                    "forced_command": f"agent-remote-attach --device {workspace.device_id}",
                }
                for ssh_key in ssh_keys
            ],
        }
        if existing is not None:
            if existing.status in {"failed", "cancelled", "expired"}:
                existing.status = "pending"
                existing.payload = payload
                existing.lease_until = None
            return task_id

        await self._repository.add_task(
            NodeTask(
                node_id=node.id,
                task_id=task_id,
                task_type="prepare_workspace",
                status="pending",
                payload=payload,
                retry_count=0,
            )
        )
        return task_id

    def _remote_workspace_path(self, *, user_id: UUID, workspace_id: UUID) -> str:
        return f"/var/lib/agent-remote/users/{user_id}/workspaces/{workspace_id}/files"

    def _mutagen_session_name(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        sync_session_id: UUID,
        node_id: UUID,
    ) -> str:
        _ = (user_id, workspace_id, node_id)
        return f"agent-remote-{sync_session_id}"

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

    def _now(self) -> datetime:
        return datetime.now(UTC)
