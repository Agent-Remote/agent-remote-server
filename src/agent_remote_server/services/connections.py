from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    Node,
    NodeTask,
    Session,
    User,
    UserDevice,
)
from agent_remote_server.repositories import NodeRepository
from agent_remote_server.repositories.connections import ConnectionRepository
from agent_remote_server.repositories.identity import IdentityRepository


@dataclass(frozen=True)
class AttachAuthorization:
    """
    SSH attach 授权结果
    """

    session: Session
    node: Node
    device: UserDevice
    tmux_session_name: str
    ssh_command: str
    command_args: list[str]
    task_id: str


class ConnectionService:
    """
    WireGuard 和 SSH 连接服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = ConnectionRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._node_repository = NodeRepository(session)

    async def get_wireguard_config(self, *, user: User, token: AuthToken) -> dict[str, object]:
        """
        读取当前设备的 WireGuard 配置

        :param user (User): 当前用户
        :param token (AuthToken): 当前 token

        :return dict: WireGuard 配置
        """

        device = await self._require_token_device(user=user, token=token)
        peer = await self._repository.get_active_device_wireguard_peer(device.id)
        if peer is None:
            raise ApiError(
                code="WIREGUARD_PEER_MISSING",
                message="Current device has no active WireGuard peer.",
                status_code=409,
            )
        nodes = await self._repository.list_connectable_nodes()
        await self._audit(
            actor_user_id=user.id,
            action="network.wireguard_config",
            target_type="user_device",
            target_id=str(device.id),
            details={"peer_count": len(nodes)},
        )
        await self._session.commit()
        return {
            "device": device,
            "peer": peer,
            "nodes": list(nodes),
        }

    async def authorize_attach(
        self, *, user: User, token: AuthToken, session_id: UUID
    ) -> AttachAuthorization:
        """
        为当前设备创建 SSH attach 授权

        :param user (User): 当前用户
        :param token (AuthToken): 当前 token
        :param session_id (UUID): session ID

        :return AttachAuthorization: attach 授权
        """

        device = await self._require_token_device(user=user, token=token)
        tool_session = await self._require_attachable_session(user=user, session_id=session_id)
        await self._ensure_workspace_sync_ready(tool_session.workspace_id)
        node = await self._require_attachable_node(tool_session.node_id)
        ssh_keys = list(await self._repository.list_active_ssh_keys_for_device(device.id))
        if not ssh_keys:
            raise ApiError(
                code="SSH_KEY_MISSING",
                message="Current device has no active SSH key.",
                status_code=409,
            )
        tmux_session_name = self._require_tmux_session(tool_session)
        ssh_user = node.ssh_user or "agent-remote"
        ssh_host = node.wireguard_ip or node.ssh_host
        if ssh_host is None:
            raise ApiError(
                code="NODE_CONNECTION_NOT_CONFIGURED",
                message="Node WireGuard or SSH host is not configured.",
                status_code=409,
            )
        ssh_port = node.ssh_port or 22
        command_args = ["agent-remote-attach", "--session", str(tool_session.id)]
        task_id = f"sync_ssh_keys:{node.id}:{device.id}:{ssh_keys[0].id}"
        forced_command = f"agent-remote-attach --session {tool_session.id} --device {device.id}"
        payload: dict[str, object] = {
            "device_id": str(device.id),
            "session_id": str(tool_session.id),
            "ssh_user": ssh_user,
            "authorized_keys_path": None,
            "ssh_keys": [
                {
                    "id": str(ssh_key.id),
                    "public_key": ssh_key.public_key,
                    "forced_command": forced_command,
                }
                for ssh_key in ssh_keys
            ],
        }
        existing = await self._node_repository.get_task_by_task_id(task_id)
        if existing is None:
            await self._node_repository.add_task(
                NodeTask(
                    node_id=node.id,
                    task_id=task_id,
                    task_type="sync_ssh_keys",
                    status="pending",
                    payload=payload,
                    retry_count=0,
                )
            )
        elif existing.status in {"failed", "cancelled", "expired"}:
            existing.status = "pending"
            existing.payload = payload
            existing.lease_until = None
        ssh_command = (
            f"ssh -p {ssh_port} {ssh_user}@{ssh_host} "
            f"agent-remote-attach --session {tool_session.id}"
        )
        await self._audit(
            actor_user_id=user.id,
            action="sessions.attach_authorize",
            target_type="session",
            target_id=str(tool_session.id),
            details={"node_id": str(node.id), "device_id": str(device.id)},
        )
        await self._session.commit()
        return AttachAuthorization(
            session=tool_session,
            node=node,
            device=device,
            tmux_session_name=tmux_session_name,
            ssh_command=ssh_command,
            command_args=command_args,
            task_id=task_id,
        )

    async def verify_node_attach(
        self, *, node: Node, node_id: UUID, session_id: UUID, device_id: UUID
    ) -> Session:
        """
        节点 forced command 入口校验 attach 授权

        :param node (Node): 当前节点
        :param node_id (UUID): 请求节点 ID
        :param session_id (UUID): session ID
        :param device_id (UUID): 设备 ID

        :return Session: 可 attach 的 session
        """

        if node.id != node_id:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Node credential does not match node.",
                status_code=403,
            )
        tool_session = await self._repository.get_session(session_id)
        if tool_session is None or tool_session.node_id != node.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        device = await self._repository.get_active_device(
            user_id=tool_session.user_id, device_id=device_id
        )
        if device is None:
            raise ApiError(code="DEVICE_REVOKED", message="Device is not active.", status_code=403)
        self._ensure_attachable_status(tool_session)
        self._require_tmux_session(tool_session)
        await self._audit(
            actor_user_id=None,
            action="node_api.attach_verify",
            target_type="session",
            target_id=str(tool_session.id),
            details={"node_id": str(node.id), "device_id": str(device.id)},
        )
        await self._session.commit()
        return tool_session

    async def _require_token_device(self, *, user: User, token: AuthToken) -> UserDevice:
        if token.user_device_id is None:
            raise ApiError(
                code="DEVICE_REQUIRED",
                message="A registered device token is required.",
                status_code=403,
            )
        device = await self._repository.get_active_device(
            user_id=user.id, device_id=token.user_device_id
        )
        if device is None:
            raise ApiError(code="DEVICE_REVOKED", message="Device is not active.", status_code=403)
        return device

    async def _require_attachable_session(self, *, user: User, session_id: UUID) -> Session:
        tool_session = await self._repository.get_session(session_id)
        if tool_session is None or tool_session.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        self._ensure_attachable_status(tool_session)
        return tool_session

    async def _ensure_workspace_sync_ready(self, workspace_id: UUID) -> None:
        blocking_sync = await self._repository.get_blocking_sync_session(workspace_id)
        if blocking_sync is not None:
            raise ApiError(
                code="SYNC_CONFLICT",
                message="Workspace sync has unresolved conflicts or failed state.",
                status_code=409,
            )

    async def _require_attachable_node(self, node_id: UUID) -> Node:
        node = await self._repository.get_node(node_id)
        if node is None:
            raise ApiError(code="COMMON_NOT_FOUND", message="Node was not found.", status_code=404)
        if node.status in {"disabled", "offline"}:
            raise ApiError(
                code="NODE_UNHEALTHY", message="Node is not attachable.", status_code=403
            )
        return node

    def _ensure_attachable_status(self, tool_session: Session) -> None:
        if tool_session.status not in {"starting", "running", "active"}:
            raise ApiError(
                code="SESSION_NOT_ATTACHABLE",
                message="Session is not attachable.",
                status_code=409,
            )

    def _require_tmux_session(self, tool_session: Session) -> str:
        if not tool_session.tmux_session_name:
            raise ApiError(
                code="SESSION_NOT_ATTACHABLE",
                message="Session has no tmux session.",
                status_code=409,
            )
        return tool_session.tmux_session_name

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
