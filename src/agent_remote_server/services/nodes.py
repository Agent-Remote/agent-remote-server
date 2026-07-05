from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    BrowserSession,
    Node,
    NodeHeartbeat,
    NodeTask,
    NodeTaskResult,
    Session,
    ToolAccount,
    ToolAccountProfile,
    User,
)
from agent_remote_server.repositories import NodeRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.security import create_opaque_token, hash_token


@dataclass(frozen=True)
class NodeRegistrationToken:
    """
    节点注册 token
    """

    node: Node
    raw_token: str


@dataclass(frozen=True)
class NodeRegistrationResult:
    """
    节点注册结果
    """

    node: Node
    raw_node_token: str


class NodeService:
    """
    节点管理和节点任务服务
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = NodeRepository(session)
        self._identity_repository = IdentityRepository(session)

    async def create_node(
        self,
        *,
        actor: User,
        name: str,
        region_code: str,
        tags: list[str],
        weight: int,
        supported_tool_types: list[str],
        wireguard_ip: str | None = None,
        wireguard_public_key: str | None = None,
        wireguard_endpoint: str | None = None,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_user: str | None = None,
    ) -> NodeRegistrationToken:
        """
        创建节点并签发注册 token

        :param actor (User): 操作人
        :param name (str): 节点名称
        :param region_code (str): 地区代码
        :param tags (list): 节点标签
        :param weight (int): 调度权重
        :param supported_tool_types (list): 支持工具类型
        :param wireguard_ip (str): WireGuard 地址
        :param wireguard_public_key (str): WireGuard 公钥
        :param wireguard_endpoint (str): WireGuard endpoint
        :param ssh_host (str): SSH 主机
        :param ssh_port (int): SSH 端口
        :param ssh_user (str): SSH 用户

        :return NodeRegistrationToken: 注册 token
        """

        raw_token = create_opaque_token("nreg")
        node = await self._repository.add_node(
            Node(
                name=name,
                status="offline",
                region_code=region_code,
                tags=tags,
                weight=weight,
                supported_tool_types=supported_tool_types,
                wireguard_ip=wireguard_ip,
                wireguard_public_key=wireguard_public_key,
                wireguard_endpoint=wireguard_endpoint,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                registration_token_hash=hash_token(self._settings.secret_key, raw_token),
            )
        )
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.create",
            target_type="node",
            target_id=str(node.id),
            details={"name": name, "region_code": region_code},
        )
        await self._session.commit()
        return NodeRegistrationToken(node=node, raw_token=raw_token)

    async def rotate_registration_token(
        self, *, actor: User, node_id: UUID
    ) -> NodeRegistrationToken:
        """
        轮换节点注册 token

        :param actor (User): 操作人
        :param node_id (UUID): 节点 ID

        :return NodeRegistrationToken: 注册 token
        """

        node = await self._require_node(node_id)
        raw_token = create_opaque_token("nreg")
        node.registration_token_hash = hash_token(self._settings.secret_key, raw_token)
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.rotate_registration_token",
            target_type="node",
            target_id=str(node.id),
            details={},
        )
        await self._session.commit()
        return NodeRegistrationToken(node=node, raw_token=raw_token)

    async def register_node(
        self,
        *,
        node_id: UUID,
        registration_token: str,
        version: str,
    ) -> NodeRegistrationResult:
        """
        节点使用注册 token 换取 node token

        :param node_id (UUID): 节点 ID
        :param registration_token (str): 注册 token
        :param version (str): 节点版本

        :return NodeRegistrationResult: 注册结果
        """

        node = await self._require_node(node_id)
        expected_hash = hash_token(self._settings.secret_key, registration_token)
        if node.registration_token_hash != expected_hash:
            raise ApiError(
                code="COMMON_UNAUTHORIZED",
                message="Invalid node registration token.",
                status_code=401,
            )
        if node.status == "disabled":
            raise ApiError(code="NODE_UNHEALTHY", message="Node is disabled.", status_code=403)

        raw_node_token = create_opaque_token("node")
        node.node_token_hash = hash_token(self._settings.secret_key, raw_node_token)
        node.registration_token_hash = None
        node.version = version
        node.status = "healthy"
        node.last_heartbeat_at = self._now()
        await self._audit(
            actor_user_id=None,
            action="node_api.register",
            target_type="node",
            target_id=str(node.id),
            details={"version": version},
        )
        await self._session.commit()
        return NodeRegistrationResult(node=node, raw_node_token=raw_node_token)

    async def authenticate_node_token(self, token: str) -> Node:
        """
        使用 node token 读取节点

        :param token (str): 原始 node token

        :return Node: 节点实体
        """

        token_hash = hash_token(self._settings.secret_key, token)
        node = await self._repository.get_node_by_token_hash(token_hash)
        if node is None:
            raise ApiError(
                code="COMMON_UNAUTHORIZED", message="Invalid node credential.", status_code=401
            )
        if node.status == "disabled":
            raise ApiError(code="NODE_UNHEALTHY", message="Node is disabled.", status_code=403)
        return node

    async def submit_heartbeat(
        self,
        *,
        node: Node,
        node_id: UUID,
        version: str,
        supported_tool_types: list[str],
        resources: dict[str, object],
        runtime: dict[str, object],
    ) -> None:
        """
        提交节点心跳

        :param node (Node): 当前节点
        :param node_id (UUID): 请求中的节点 ID
        :param version (str): 节点版本
        :param supported_tool_types (list): 支持工具类型
        :param resources (dict): 资源快照
        :param runtime (dict): 运行时快照
        """

        if node.id != node_id:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Node credential does not match node.",
                status_code=403,
            )
        now = self._now()
        node.version = version
        node.supported_tool_types = supported_tool_types
        node.last_heartbeat_at = now
        node.status = (
            "healthy" if runtime.get("docker_ok") and runtime.get("tmux_ok") else "degraded"
        )
        await self._repository.add_heartbeat(
            NodeHeartbeat(
                node_id=node.id,
                version=version,
                supported_tool_types=supported_tool_types,
                resources=resources,
                runtime=runtime,
            )
        )
        await self._session.commit()

    async def list_nodes(self) -> list[Node]:
        """
        列出节点并标记过期离线

        :return list: 节点列表
        """

        nodes = list(await self._repository.list_nodes())
        changed = self._mark_stale_nodes(nodes)
        if changed:
            await self._session.commit()
        return nodes

    async def get_node(self, node_id: UUID) -> Node:
        """
        读取节点并标记过期离线

        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        node = await self._require_node(node_id)
        if self._mark_stale_nodes([node]):
            await self._session.commit()
        return node

    async def update_node(
        self,
        *,
        actor: User,
        node_id: UUID,
        name: str | None,
        status: str | None,
        tags: list[str] | None,
        weight: int | None,
        supported_tool_types: list[str] | None,
        wireguard_ip: str | None,
        wireguard_public_key: str | None,
        wireguard_endpoint: str | None,
        ssh_host: str | None,
        ssh_port: int | None,
        ssh_user: str | None,
    ) -> Node:
        """
        更新节点

        :param actor (User): 操作人
        :param node_id (UUID): 节点 ID
        :param name (str): 节点名称
        :param status (str): 节点状态
        :param tags (list): 节点标签
        :param weight (int): 权重
        :param supported_tool_types (list): 支持工具类型
        :param wireguard_ip (str): WireGuard 地址
        :param wireguard_public_key (str): WireGuard 公钥
        :param wireguard_endpoint (str): WireGuard endpoint
        :param ssh_host (str): SSH 主机
        :param ssh_port (int): SSH 端口
        :param ssh_user (str): SSH 用户

        :return Node: 节点实体
        """

        node = await self._require_node(node_id)
        if name is not None:
            node.name = name
        if status is not None:
            node.status = status
        if tags is not None:
            node.tags = tags
        if weight is not None:
            node.weight = weight
        if supported_tool_types is not None:
            node.supported_tool_types = supported_tool_types
        if wireguard_ip is not None:
            node.wireguard_ip = wireguard_ip
        if wireguard_public_key is not None:
            node.wireguard_public_key = wireguard_public_key
        if wireguard_endpoint is not None:
            node.wireguard_endpoint = wireguard_endpoint
        if ssh_host is not None:
            node.ssh_host = ssh_host
        if ssh_port is not None:
            node.ssh_port = ssh_port
        if ssh_user is not None:
            node.ssh_user = ssh_user
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.update",
            target_type="node",
            target_id=str(node.id),
            details={"status": status} if status else {},
        )
        await self._session.commit()
        return node

    async def set_maintenance(self, *, actor: User, node_id: UUID) -> Node:
        """
        设置节点维护状态

        :param actor (User): 操作人
        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        return await self.update_node(
            actor=actor,
            node_id=node_id,
            name=None,
            status="maintenance",
            tags=None,
            weight=None,
            supported_tool_types=None,
            wireguard_ip=None,
            wireguard_public_key=None,
            wireguard_endpoint=None,
            ssh_host=None,
            ssh_port=None,
            ssh_user=None,
        )

    async def disable_node(self, *, actor: User, node_id: UUID) -> Node:
        """
        禁用节点

        :param actor (User): 操作人
        :param node_id (UUID): 节点 ID

        :return Node: 节点实体
        """

        node = await self.update_node(
            actor=actor,
            node_id=node_id,
            name=None,
            status="disabled",
            tags=None,
            weight=None,
            supported_tool_types=None,
            wireguard_ip=None,
            wireguard_public_key=None,
            wireguard_endpoint=None,
            ssh_host=None,
            ssh_port=None,
            ssh_user=None,
        )
        node.node_token_hash = None
        node.registration_token_hash = None
        await self._session.commit()
        return node

    async def create_task(
        self,
        *,
        node_id: UUID,
        task_id: str,
        task_type: str,
        payload: dict[str, object],
    ) -> NodeTask:
        """
        幂等创建节点任务

        :param node_id (UUID): 节点 ID
        :param task_id (str): 任务 ID
        :param task_type (str): 任务类型
        :param payload (dict): 任务 payload

        :return NodeTask: 节点任务
        """

        existing = await self._repository.get_task_by_task_id(task_id)
        if existing is not None:
            return existing
        task = await self._repository.add_task(
            NodeTask(
                node_id=node_id,
                task_id=task_id,
                task_type=task_type,
                status="pending",
                payload=payload,
                retry_count=0,
            )
        )
        await self._session.commit()
        return task

    async def poll_tasks(self, *, node: Node, limit: int = 1) -> list[NodeTask]:
        """
        租约节点任务

        :param node (Node): 当前节点
        :param limit (int): 最大任务数

        :return list: 节点任务列表
        """

        now = self._now()
        lease_until = now + timedelta(seconds=self._settings.node_task_lease_seconds)
        tasks = list(
            await self._repository.list_pollable_tasks(node_id=node.id, now=now, limit=limit)
        )
        for task in tasks:
            task.status = "leased"
            task.lease_until = lease_until
            task.retry_count += 1
        if tasks:
            await self._session.commit()
        return tasks

    async def start_task(self, *, node: Node, task_id: str) -> None:
        """
        标记任务开始

        :param node (Node): 当前节点
        :param task_id (str): 任务 ID
        """

        task = await self._require_node_task(node=node, task_id=task_id)
        if task.status in {"succeeded", "failed", "cancelled", "expired"}:
            raise ApiError(
                code="COMMON_CONFLICT", message="Task is already terminal.", status_code=409
            )
        task.status = "running"
        await self._session.commit()

    async def complete_task(self, *, node: Node, task_id: str, result: dict[str, object]) -> None:
        """
        完成任务

        :param node (Node): 当前节点
        :param task_id (str): 任务 ID
        :param result (dict): 任务结果
        """

        task = await self._require_node_task(node=node, task_id=task_id)
        if await self._repository.get_task_result(task_id) is None:
            await self._repository.add_task_result(
                NodeTaskResult(
                    node_task_id=task.id,
                    task_id=task.task_id,
                    status="succeeded",
                    result=result,
                    error=None,
                    started_at=None,
                    finished_at=self._now(),
                )
            )
        task.status = "succeeded"
        await self._apply_tool_account_task_result(task, result)
        await self._apply_tool_session_task_result(task, result)
        await self._apply_browser_session_task_result(task, result)
        await self._session.commit()

    async def fail_task(self, *, node: Node, task_id: str, error: dict[str, object]) -> None:
        """
        标记任务失败

        :param node (Node): 当前节点
        :param task_id (str): 任务 ID
        :param error (dict): 错误信息
        """

        task = await self._require_node_task(node=node, task_id=task_id)
        if await self._repository.get_task_result(task_id) is None:
            await self._repository.add_task_result(
                NodeTaskResult(
                    node_task_id=task.id,
                    task_id=task.task_id,
                    status="failed",
                    result=None,
                    error=error,
                    started_at=None,
                    finished_at=self._now(),
                )
            )
        task.status = "failed"
        await self._apply_tool_account_task_failure(task, error)
        await self._apply_tool_session_task_failure(task, error)
        await self._apply_browser_session_task_failure(task, error)
        await self._session.commit()

    async def reconcile(
        self, *, node: Node, node_id: UUID, sections: list[str], snapshot: dict[str, object]
    ) -> None:
        """
        接收节点对账快照

        :param node (Node): 当前节点
        :param node_id (UUID): 请求节点 ID
        :param sections (list): 对账分区
        :param snapshot (dict): 对账快照
        """

        if node.id != node_id:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Node credential does not match node.",
                status_code=403,
            )
        await self._audit(
            actor_user_id=None,
            action="node_api.reconcile",
            target_type="node",
            target_id=str(node.id),
            details={"sections": sections, "snapshot_keys": sorted(snapshot)},
        )
        await self._session.commit()

    async def _require_node(self, node_id: UUID) -> Node:
        node = await self._repository.get_node(node_id)
        if node is None:
            raise ApiError(code="COMMON_NOT_FOUND", message="Node was not found.", status_code=404)
        return node

    async def _require_node_task(self, *, node: Node, task_id: str) -> NodeTask:
        task = await self._repository.get_task_by_task_id(task_id)
        if task is None or task.node_id != node.id:
            raise ApiError(code="COMMON_NOT_FOUND", message="Task was not found.", status_code=404)
        return task

    async def _apply_tool_account_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        account_id = self._task_tool_account_id(task, result)
        if account_id is None:
            return
        account = await self._session.get(ToolAccount, account_id)
        if account is None:
            return
        profile = await self._tool_account_profile(account)
        if task.task_type == "create_binding_session":
            status = result.get("status")
            if status in {"waiting_user_login", "ready"}:
                account.status = "binding_waiting_user_login"
                profile.profile_json = {
                    **profile.profile_json,
                    "binding_session_id": self._text_result(result, "binding_session_id"),
                    "tmux_session_name": self._text_result(result, "tmux_session_name"),
                    "account_remote_path": self._text_result(result, "account_remote_path"),
                    "last_binding_result": result,
                }
                return
            account.status = "failed"
            profile.profile_json = {
                **profile.profile_json,
                "last_error": self._text_result(result, "error") or "Binding session failed.",
            }
            return
        if task.task_type == "verify_tool_account":
            if result.get("verified") is True:
                account.status = "active"
                profile.profile_json = {
                    **profile.profile_json,
                    "account_remote_path": self._text_result(result, "account_remote_path"),
                    "verified_at": self._now().isoformat(),
                    "verifier_metadata": result.get("metadata")
                    if isinstance(result.get("metadata"), dict)
                    else {},
                    "last_error": None,
                }
                return
            account.status = "failed"
            profile.profile_json = {
                **profile.profile_json,
                "last_error": self._text_result(result, "error")
                or "Tool account verification failed.",
            }

    async def _apply_tool_account_task_failure(
        self, task: NodeTask, error: dict[str, object]
    ) -> None:
        account_id = self._task_tool_account_id(task, error)
        if account_id is None:
            return
        account = await self._session.get(ToolAccount, account_id)
        if account is None:
            return
        profile = await self._tool_account_profile(account)
        account.status = "failed"
        profile.profile_json = {
            **profile.profile_json,
            "last_error": self._text_result(error, "message") or self._text_result(error, "error"),
        }

    async def _apply_tool_session_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        session_id = self._task_session_id(task, result)
        if session_id is None:
            return
        tool_session = await self._session.get(Session, session_id)
        if tool_session is None:
            return
        if task.task_type == "create_tool_session":
            status = result.get("status")
            if status in {"running", "active", "ready"}:
                tool_session.status = "running" if status == "running" else "active"
                tmux_session_name = self._text_result(result, "tmux_session_name")
                container_id = self._text_result(result, "container_id") or self._text_result(
                    result, "sandbox_name"
                )
                if tmux_session_name is not None:
                    tool_session.tmux_session_name = tmux_session_name
                if container_id is not None:
                    tool_session.container_id = container_id
                return
            tool_session.status = "failed"
            return
        if task.task_type == "stop_tool_session":
            tool_session.status = "stopped"

    async def _apply_tool_session_task_failure(
        self, task: NodeTask, error: dict[str, object]
    ) -> None:
        session_id = self._task_session_id(task, error)
        if session_id is None:
            return
        tool_session = await self._session.get(Session, session_id)
        if tool_session is None:
            return
        if task.task_type == "stop_tool_session":
            tool_session.status = "failed"
            return
        if task.task_type == "create_tool_session":
            tool_session.status = "failed"

    async def _apply_browser_session_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        browser_session_id = self._task_browser_session_id(task, result)
        if browser_session_id is None:
            return
        browser_session = await self._session.get(BrowserSession, browser_session_id)
        if browser_session is None:
            return
        if task.task_type == "create_browser_session":
            status = result.get("status")
            if status == "ready":
                browser_session.status = "ready"
                container_id = self._text_result(result, "container_id") or self._text_result(
                    result, "container_name"
                )
                stream_endpoint = self._text_result(result, "stream_endpoint")
                if container_id is not None:
                    browser_session.container_id = container_id
                if stream_endpoint is not None:
                    browser_session.stream_endpoint = stream_endpoint
                return
            browser_session.status = "failed"
            return
        if task.task_type == "stop_browser_session":
            browser_session.status = "stopped"
            browser_session.stopped_at = self._now()

    async def _apply_browser_session_task_failure(
        self, task: NodeTask, error: dict[str, object]
    ) -> None:
        browser_session_id = self._task_browser_session_id(task, error)
        if browser_session_id is None:
            return
        browser_session = await self._session.get(BrowserSession, browser_session_id)
        if browser_session is None:
            return
        if task.task_type in {"create_browser_session", "stop_browser_session"}:
            browser_session.status = "failed"
            browser_session.stopped_at = self._now()

    async def _tool_account_profile(self, account: ToolAccount) -> ToolAccountProfile:
        profile = await self._session.scalar(
            select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == account.id)
        )
        if profile is not None:
            return profile
        profile = ToolAccountProfile(
            tool_account_id=account.id,
            tool_type=account.tool_type,
            profile_json={},
            encrypted_secrets=None,
        )
        self._session.add(profile)
        await self._session.flush()
        return profile

    def _task_tool_account_id(self, task: NodeTask, fallback: dict[str, object]) -> UUID | None:
        value = task.payload.get("tool_account_id") or fallback.get("tool_account_id")
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    def _task_session_id(self, task: NodeTask, fallback: dict[str, object]) -> UUID | None:
        value = task.payload.get("session_id") or fallback.get("session_id")
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    def _task_browser_session_id(self, task: NodeTask, fallback: dict[str, object]) -> UUID | None:
        value = task.payload.get("browser_session_id") or fallback.get("browser_session_id")
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    def _text_result(self, result: dict[str, object], key: str) -> str | None:
        value = result.get(key)
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

    def _mark_stale_nodes(self, nodes: list[Node]) -> bool:
        changed = False
        cutoff = self._now() - timedelta(seconds=self._settings.node_offline_after_seconds)
        for node in nodes:
            if node.status in {"disabled", "maintenance"} or node.last_heartbeat_at is None:
                continue
            heartbeat_at = (
                node.last_heartbeat_at
                if node.last_heartbeat_at.tzinfo
                else node.last_heartbeat_at.replace(tzinfo=UTC)
            )
            if heartbeat_at < cutoff:
                node.status = "offline"
                changed = True
        return changed

    def _now(self) -> datetime:
        return datetime.now(UTC)
