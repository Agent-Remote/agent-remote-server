"""
为固定账户绑定尝试授权设备 SSH，并只读观察原同步任务。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, AuthToken, NodeTask, User
from agent_remote_server.repositories.connections import ConnectionRepository
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.tool_accounts import ToolAccountRepository
from agent_remote_server.schemas.binding_attach import BindingAttachData
from agent_remote_server.services.ssh_keys import ssh_key_sync_payload, ssh_key_sync_task_id


class BindingAttachService:
    """
    验证账户、设备、节点和绑定尝试后处理固定 SSH 同步任务。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定本次请求事务。

        :param session (AsyncSession): 本次请求事务
        """
        self._session = session
        self._accounts = ToolAccountRepository(session)
        self._connections = ConnectionRepository(session)
        self._nodes = NodeRepository(session)

    async def resolve(
        self,
        *,
        user: User,
        token: AuthToken,
        account_id: UUID,
        expected_binding_session_id: str,
        task_id: str | None,
    ) -> BindingAttachData:
        """
        显式授权或只读查询已绑定的 SSH 同步任务。

        :param user (User): 已认证用户
        :param token (AuthToken): 当前设备凭据
        :param account_id (UUID): 原账户身份
        :param expected_binding_session_id (str): 原绑定尝试
        :param task_id (str | None): GET 的原任务身份，空值仅用于 POST
        :return BindingAttachData: 结构化连接和任务观察
        """
        account = await self._accounts.get_account_for_update(account_id)
        if account is None or account.user_id != user.id:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Tool account was not found.", status_code=404
            )
        if token.user_device_id is None:
            raise ApiError(
                code="DEVICE_REQUIRED", message="A registered device is required.", status_code=403
            )
        device = await self._connections.get_active_device(
            user_id=user.id, device_id=token.user_device_id
        )
        if device is None:
            raise ApiError(code="DEVICE_REVOKED", message="Device is not active.", status_code=403)
        if account.status not in {"binding_session_starting", "binding_waiting_user_login"}:
            raise ApiError(
                code="SESSION_NOT_ATTACHABLE", message="Binding is not attachable.", status_code=409
            )
        profile = await self._accounts.get_profile(account.id)
        values = profile.profile_json if profile is not None else {}
        if values.get("binding_session_id") != expected_binding_session_id:
            raise ApiError(
                code="BINDING_SESSION_CHANGED", message="Binding attempt changed.", status_code=409
            )
        tmux = values.get("tmux_session_name")
        if not isinstance(tmux, str) or not tmux or len(tmux) > 128:
            raise ApiError(
                code="SESSION_NOT_READY", message="Binding terminal is not ready.", status_code=409
            )
        node = (
            await self._connections.get_node(account.affinity_node_id)
            if account.affinity_node_id
            else None
        )
        if node is None or node.status not in {"healthy", "degraded"}:
            raise ApiError(
                code="NODE_UNAVAILABLE", message="Binding node is not available.", status_code=409
            )
        capability = node.runtime_capabilities.get("binding_attach")
        if (
            not isinstance(capability, dict)
            or capability.get("supported") is not True
            or type(capability.get("protocol_version")) is not int
            or capability.get("protocol_version") != 1
        ):
            raise ApiError(
                code="NODE_BINDING_ATTACH_UNSUPPORTED",
                message="Upgrade the Node and installed SSH gateway for exact binding attachment.",
                status_code=409,
            )
        host = node.ssh_host or node.wireguard_ip
        if not host:
            raise ApiError(
                code="NODE_CONNECTION_NOT_CONFIGURED",
                message="Node SSH host is not configured.",
                status_code=409,
            )
        ssh_user = node.ssh_user or "agent-remote"
        keys = list(await self._connections.list_active_ssh_keys_for_device(device.id))
        if not keys:
            raise ApiError(
                code="SSH_KEY_MISSING",
                message="Current device has no active SSH key.",
                status_code=409,
            )
        expected_task = ssh_key_sync_task_id(node_id=node.id, device_id=device.id, ssh_keys=keys)
        if task_id is not None and task_id != expected_task:
            raise ApiError(
                code="BINDING_AUTHORIZATION_CHANGED",
                message="SSH authorization identity changed.",
                status_code=409,
            )
        payload = ssh_key_sync_payload(device_id=device.id, ssh_user=ssh_user, ssh_keys=keys)
        task = await self._nodes.get_task_by_task_id(expected_task)
        if task is not None and (
            task.node_id != node.id or task.task_type != "sync_ssh_keys" or task.payload != payload
        ):
            raise ApiError(
                code="BINDING_AUTHORIZATION_CHANGED",
                message="SSH authorization payload changed.",
                status_code=409,
            )
        if task is None:
            if task_id is not None:
                raise ApiError(
                    code="COMMON_NOT_FOUND",
                    message="SSH authorization task was not found.",
                    status_code=404,
                )
            task = await self._nodes.add_task(
                NodeTask(
                    node_id=node.id,
                    task_id=expected_task,
                    task_type="sync_ssh_keys",
                    status="pending",
                    payload=payload,
                    retry_count=0,
                )
            )
        data = BindingAttachData(
            tool_account_id=account.id,
            binding_session_id=expected_binding_session_id,
            node_id=node.id,
            device_id=device.id,
            ssh_host=host,
            ssh_port=node.ssh_port or 22,
            ssh_user=ssh_user,
            tmux_session_name=tmux,
            authorization_task_id=task.task_id,
            authorization_task_status=task.status,
        )
        if task_id is None:
            self._session.add(
                AuditLog(
                    actor_user_id=user.id,
                    action="tool_accounts.bind.attach_authorize",
                    target_type="tool_account",
                    target_id=str(account.id),
                    details={
                        "node_id": str(node.id),
                        "device_id": str(device.id),
                        "binding_session_id": expected_binding_session_id,
                    },
                )
            )
            await self._session.commit()
        return data
