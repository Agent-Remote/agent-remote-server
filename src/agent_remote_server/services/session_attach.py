"""
为固定 Claude 会话、设备和授权任务提供独立精确挂接服务。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, AuthToken, Node, NodeTask, Session, SshKey, User
from agent_remote_server.repositories.connections import ConnectionRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.schemas.session_attach import (
    ExactSessionAttachData,
    VerifyExactSessionAttachData,
    VerifyExactSessionAttachRequest,
)
from agent_remote_server.services.connections import ConnectionService
from agent_remote_server.services.ssh_keys import ssh_key_sync_payload, ssh_key_sync_task_id


class ExactSessionAttachService:
    """
    重验完整挂接权限，查询原任务时不修改任何业务或活动记录。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        绑定调用方事务及连接策略。

        :param session (AsyncSession): 请求事务
        :param settings (Settings): 应用配置
        """
        self.session = session
        self.connections = ConnectionRepository(session)
        self.nodes = NodeRepository(session)
        self.identity = IdentityRepository(session)
        self.policy = ConnectionService(session, settings)

    async def context(
        self, owner: UUID, session_id: UUID, device_id: UUID
    ) -> tuple[Session, Node, list[SshKey]]:
        """
        只读核对当前会话、用户、设备、密钥和安装网关能力。

        :param owner (UUID): 必须匹配的活动用户
        :param session_id (UUID): 原会话身份
        :param device_id (UUID): 已绑定设备身份
        :return tuple[Session, Node, list[SshKey]]: 已验证会话、节点与当前密钥集
        """
        user = await self.identity.get_user(owner)
        if user is None or user.status != "active":
            raise attach_error("COMMON_FORBIDDEN", 403)
        runtime = await self.connections.get_session(session_id)
        if runtime is None or runtime.user_id != owner:
            raise attach_error("COMMON_NOT_FOUND", 404)
        if runtime.tool_type != "claude":
            raise attach_error("SESSION_TOOL_UNSUPPORTED")
        if (
            runtime.status not in {"running", "active"}
            or not runtime.tmux_session_name
            or runtime.runtime_backend not in {"native", "docker_sandbox"}
        ):
            raise attach_error("SESSION_NOT_ATTACHABLE")
        device = await self.connections.get_active_device(user_id=owner, device_id=device_id)
        if device is None:
            raise attach_error("DEVICE_REVOKED", 403)
        node = await self.connections.get_node(runtime.node_id)
        if node is None or node.status not in {"healthy", "degraded"}:
            raise attach_error("NODE_UNAVAILABLE")
        capability = node.runtime_capabilities.get("session_attach")
        if (
            not isinstance(capability, dict)
            or capability.get("supported") is not True
            or type(capability.get("protocol_version")) is not int
            or capability.get("protocol_version") != 1
        ):
            raise attach_error("NODE_SESSION_ATTACH_UNSUPPORTED")
        if await self.connections.get_blocking_sync_session(runtime.workspace_id) is not None:
            raise attach_error("SYNC_CONFLICT")
        keys = list(await self.connections.list_active_ssh_keys_for_device(device_id))
        if not keys:
            raise attach_error("SSH_KEY_MISSING")
        return runtime, node, keys

    async def resolve(
        self,
        user: User,
        token: AuthToken,
        session_id: UUID,
        expected_device_id: UUID,
        task_id: str | None = None,
        task_record_id: UUID | None = None,
    ) -> ExactSessionAttachData:
        """
        单次授权可创建原密钥同步任务，后续观察必须精确匹配原任务行。

        :param user (User): 当前活动用户
        :param token (AuthToken): 已验证设备令牌
        :param session_id (UUID): 原会话身份
        :param expected_device_id (UUID): 已确认本机设备身份
        :param task_id (str | None): 查询时要求的原逻辑任务
        :param task_record_id (UUID | None): 查询时要求的原数据库任务行
        :return ExactSessionAttachData: 当前授权和原任务观察
        """
        if (
            token.token_type != "device"
            or token.user_id != user.id
            or token.user_device_id != expected_device_id
        ):
            raise attach_error("SESSION_ATTACH_DEVICE_CHANGED", 403)
        runtime, node, keys = await self.context(user.id, session_id, expected_device_id)
        host = node.wireguard_ip or node.ssh_host
        if not host:
            raise attach_error("NODE_CONNECTION_NOT_CONFIGURED")
        ssh_user = node.ssh_user or "agent-remote"
        expected_task = ssh_key_sync_task_id(
            node_id=node.id, device_id=expected_device_id, ssh_keys=keys
        )
        payload = ssh_key_sync_payload(
            device_id=expected_device_id, ssh_user=ssh_user, ssh_keys=keys
        )
        if task_id is not None and (task_id != expected_task or task_record_id is None):
            raise attach_error("SESSION_AUTHORIZATION_CHANGED")
        task = await self.nodes.get_task_by_task_id(expected_task)
        if task is not None and (
            task.node_id != node.id
            or task.task_type != "sync_ssh_keys"
            or task.payload != payload
            or (task_id is not None and task.id != task_record_id)
        ):
            raise attach_error("SESSION_AUTHORIZATION_CHANGED")
        if task is None:
            if task_id is not None:
                raise attach_error("COMMON_NOT_FOUND", 404)
            task = await self.nodes.add_task(
                NodeTask(
                    node_id=node.id,
                    task_id=expected_task,
                    task_type="sync_ssh_keys",
                    status="pending",
                    payload=payload,
                    retry_count=0,
                )
            )
        data = ExactSessionAttachData.model_validate(
            {
                "session_id": runtime.id,
                "node_id": node.id,
                "device_id": expected_device_id,
                "ssh_host": host,
                "ssh_port": node.ssh_port or 22,
                "ssh_user": ssh_user,
                "tmux_session_name": runtime.tmux_session_name,
                "forward_ssh_agent": await self.policy.allows_ssh_agent_forwarding(runtime),
                "authorization_task_id": task.task_id,
                "authorization_task_record_id": task.id,
                "authorization_task_status": task.status,
            }
        )
        if task_id is None:
            self.session.add(
                AuditLog(
                    actor_user_id=user.id,
                    action="sessions.attach_exact_authorize",
                    target_type="session",
                    target_id=str(runtime.id),
                    details={"node_id": str(node.id), "device_id": str(expected_device_id)},
                )
            )
            await self.session.commit()
        return data

    async def verify(
        self, node: Node, payload: VerifyExactSessionAttachRequest
    ) -> VerifyExactSessionAttachData:
        """
        校验节点实际认证密钥并返回完整身份回显。

        :param node (Node): 已认证节点
        :param payload (VerifyExactSessionAttachRequest): 强制命令提供的四个身份
        :return VerifyExactSessionAttachData: 最小运行时投影与身份回显
        """
        if node.id != payload.node_id:
            raise attach_error("COMMON_FORBIDDEN", 403)
        runtime = await self.connections.get_session(payload.session_id)
        if runtime is None or runtime.node_id != node.id:
            raise attach_error("COMMON_NOT_FOUND", 404)
        runtime, _, keys = await self.context(runtime.user_id, runtime.id, payload.device_id)
        if not any(key.id == payload.ssh_key_id for key in keys):
            raise attach_error("SSH_KEY_REVOKED", 403)
        data = VerifyExactSessionAttachData.model_validate(
            {
                **payload.model_dump(),
                "tmux_session_name": runtime.tmux_session_name,
                "container_id": runtime.container_id,
                "runtime_backend": runtime.runtime_backend,
                "runtime_resource_id": runtime.runtime_resource_id,
                "forward_ssh_agent": await self.policy.allows_ssh_agent_forwarding(runtime),
            }
        )
        self.session.add(
            AuditLog(
                actor_user_id=None,
                action="node_api.attach_exact_verify",
                target_type="session",
                target_id=str(runtime.id),
                details={"node_id": str(node.id), "device_id": str(payload.device_id)},
            )
        )
        await self.session.commit()
        return data


def attach_error(code: str, status: int = 409) -> ApiError:
    """
    返回不包含密钥或历史任务内容的固定拒绝消息。

    :param code (str): 稳定错误码
    :param status (int): HTTP 状态
    :return ApiError: 精确连接拒绝结果
    """
    return ApiError(
        code=code, message="Exact session attachment is unavailable.", status_code=status
    )
