"""
实现会话业务逻辑。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    SessionCreateRequest,
    SessionDeleteRequest,
    SessionStopRequest,
    ToolAccount,
    User,
    Workspace,
)
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.repositories.session_create_requests import SessionCreateRequestRepository
from agent_remote_server.repositories.session_delete_requests import SessionDeleteRequestRepository
from agent_remote_server.repositories.session_stop_requests import SessionStopRequestRepository
from agent_remote_server.repositories.sessions import SessionRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.device_sessions import (
    DeviceSessionService,
    DeviceSessionStopResult,
    RevokedDeviceBinding,
)
from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.port_forward_revocation import revoke_port_forwards
from agent_remote_server.services.runtime_migrations import require_runtime_migration_settled
from agent_remote_server.services.session_attachment import SessionAttachmentService
from agent_remote_server.services.session_deletion import require_settled_session_stops
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.lifecycle import existing_history_mutations
from agent_remote_server.services.skills.runtime_capability import supports_managed_skills
from agent_remote_server.services.skills.session_admission import (
    SkillAdmissionPending,
    SkillSessionAdmission,
)
from agent_remote_server.services.skills.session_retention import release_retained_session_reference
from agent_remote_server.services.skills.stop_status import SkillStopStatusService
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
StopSessionLockedResult = tuple[Session, DeviceSessionStopResult | None, EgoBrowserService]


@dataclass(frozen=True)
class SessionCreateCaller:
    """
    已规范化的调用方创建意图，仅包含无内容身份摘要。
    """

    request_id: UUID
    schema_version: int
    argv_digest: str
    intent_fingerprint: str
    sync_session_id: UUID | None
    sync_target: dict[str, object] | None


@dataclass(frozen=True)
class SessionCreateObservation:
    """
    原键当前只读观察。
    """

    receipt: dict[str, object]
    observation: dict[str, object]


@dataclass(frozen=True)
class SessionStopObservation:
    """
    停止原键当前只读观察。
    """

    receipt: dict[str, object]
    observation: dict[str, object]


@dataclass(frozen=True)
class SessionDeleteObservation:
    """
    固定集合删除原键当前只读观察。
    """

    receipt: dict[str, object]
    observation: dict[str, object]


def _digest(value: object) -> str:
    """
    以无空白 UTF-8 JSON 计算调用方意图摘要。

    :param value (object): 已规范化的无内容意图
    :return str: 带 sha256 前缀的摘要
    """
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _valid_digest(value: str | None) -> bool:
    """
    检查外部摘要是否为规范 sha256 文本。

    :param value (str | None): 外部摘要
    :return bool: 是否有效
    """
    return (
        value is not None
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


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

    def _caller_intent(
        self,
        *,
        request_id: UUID | None,
        schema_version: int | None,
        tool_type: str,
        tool_account_id: UUID,
        workspace_id: UUID,
        project_key: str,
        argv: list[str],
        replaces_session_id: UUID | None,
        argv_digest: str | None,
        intent_fingerprint: str | None,
        sync_target: dict[str, object] | None,
    ) -> SessionCreateCaller | None:
        """
        规范化调用方键；旧客户端不带键时保持原有创建路径。

        :param request_id (UUID | None): 调用方原始请求键
        :param schema_version (int | None): 调用方协议版本
        :param tool_type (str): 工具类型
        :param tool_account_id (UUID): 工具账户身份
        :param workspace_id (UUID): 工作区身份
        :param project_key (str): 项目身份
        :param argv (list[str]): CLI 参数
        :param replaces_session_id (UUID | None): 替代会话身份
        :param argv_digest (str | None): 外部参数摘要
        :param intent_fingerprint (str | None): 外部意图摘要
        :param sync_target (dict[str, object] | None): 外部同步目标
        :return SessionCreateCaller | None: 规范化调用方意图
        :raises ApiError: 调用方键字段不完整或摘要不合法
        """
        if request_id is None:
            return None
        if schema_version != 1:
            raise ApiError(
                code="SESSION_CREATE_SCHEMA_UNSUPPORTED",
                message="Session create request schema is unsupported.",
                status_code=422,
            )
        computed_argv = _digest(argv)
        if argv_digest is None:
            argv_digest = computed_argv
        if not _valid_digest(argv_digest):
            raise ApiError(
                code="SESSION_CREATE_ARGV_DIGEST_INVALID",
                message="Session create argv digest is invalid.",
                status_code=422,
            )
        if argv_digest != computed_argv:
            raise ApiError(
                code="SESSION_CREATE_ARGV_DIGEST_MISMATCH",
                message="Session create argv digest does not match the submitted arguments.",
                status_code=409,
            )
        identity = {
            "schema_version": 1,
            "tool_type": tool_type,
            "tool_account_id": str(tool_account_id),
            "workspace_id": str(workspace_id),
            "project_key": project_key,
            "argv_digest": argv_digest,
            "replacement_session_id": str(replaces_session_id)
            if replaces_session_id is not None
            else None,
            "sync_target": sync_target,
        }
        computed_intent = _digest(identity)
        if intent_fingerprint is None:
            intent_fingerprint = computed_intent
        if not _valid_digest(intent_fingerprint):
            raise ApiError(
                code="SESSION_CREATE_INTENT_DIGEST_INVALID",
                message="Session create intent digest is invalid.",
                status_code=422,
            )
        if intent_fingerprint != computed_intent:
            raise ApiError(
                code="SESSION_CREATE_INTENT_MISMATCH",
                message="Session create intent digest does not match the submitted intent.",
                status_code=409,
            )
        sync_session_id = None
        if sync_target is not None and sync_target.get("id") is not None:
            raw_sync_id = sync_target["id"]
            try:
                sync_session_id = (
                    raw_sync_id if isinstance(raw_sync_id, UUID) else UUID(str(raw_sync_id))
                )
            except (TypeError, ValueError) as error:
                raise ApiError(
                    code="SESSION_CREATE_SYNC_TARGET_INVALID",
                    message="Session create sync target is invalid.",
                    status_code=422,
                ) from error
        return SessionCreateCaller(
            request_id=request_id,
            schema_version=1,
            argv_digest=argv_digest,
            intent_fingerprint=intent_fingerprint,
            sync_session_id=sync_session_id,
            sync_target=sync_target,
        )

    async def _replay_create_request(
        self, user: User, record: SessionCreateRequest, intent_fingerprint: str
    ) -> Session:
        """
        复用同键原始受理结果，拒绝同键异意图和删除后猜测新会话。

        :param user (User): 当前用户
        :param record (SessionCreateRequest): 原始受理记录
        :param intent_fingerprint (str): 本次请求摘要
        :return Session: 原始会话
        :raises ApiError: 意图冲突或原始会话已不存在
        """
        if record.intent_fingerprint != intent_fingerprint:
            raise ApiError(
                code="SESSION_CREATE_REQUEST_CONFLICT",
                message="Request identity is already bound to a different session create.",
                status_code=409,
            )
        if record.session_id is None:
            raise ApiError(
                code=record.acceptance_code or "SESSION_CREATE_ADMISSION_PENDING",
                message="The original session create request is still pending.",
                status_code=409,
                details=record.receipt_json,
            )
        tool_session = await self._repository.get_session(record.session_id)
        if tool_session is None or tool_session.user_id != user.id:
            raise ApiError(
                code="SESSION_CREATE_REQUEST_ACCEPTED",
                message="The original session was already deleted; recover it by request key.",
                status_code=409,
                details=record.receipt_json,
            )
        return tool_session

    def _receipt_for_session(
        self,
        *,
        caller: SessionCreateCaller,
        session: Session,
        task: NodeTask | None,
    ) -> dict[str, object]:
        """
        构建稳定的原始受理收据，不写入命令参数。

        :param caller (SessionCreateCaller): 规范化调用方意图
        :param session (Session): 新会话
        :param task (NodeTask | None): 创建任务
        :return dict[str, object]: JSON 可序列化受理凭据
        """
        return {
            "schema_version": 1,
            "request_id": str(caller.request_id),
            "tool_type": session.tool_type,
            "tool_account_id": str(session.tool_account_id),
            "workspace_id": str(session.workspace_id),
            "project_key": session.project_key,
            "argv_digest": caller.argv_digest,
            "intent_fingerprint": caller.intent_fingerprint,
            "replacement_session_id": str(session.replaces_session_id)
            if session.replaces_session_id is not None
            else None,
            "sync_target_digest": (
                caller.sync_target.get("target_digest") if caller.sync_target is not None else None
            ),
            "node_id": str(session.node_id),
            "session_id": str(session.id),
            "create_task_id": task.task_id if task is not None else None,
            "task_record_id": str(task.id) if task is not None else None,
            "phase": "accepted",
            "acceptance_code": None,
        }

    async def get_create_request(self, *, user: User, request_id: UUID) -> SessionCreateObservation:
        """
        仅按所有者和原键读取不可变收据及当前观察。

        :param user (User): 当前用户
        :param request_id (UUID): 调用方原始请求键
        :return SessionCreateObservation: 收据与当前观察
        :raises ApiError: 原键不存在
        """
        repository = SessionCreateRequestRepository(self._session)
        record = await repository.get(user.id, request_id)
        if record is None:
            raise ApiError(
                code="SESSION_CREATE_REQUEST_NOT_FOUND",
                message="Session create request was not found.",
                status_code=404,
            )
        session_status = None
        session_deleted = False
        if record.session_id is not None:
            current = await self._repository.get_session(record.session_id)
            if current is None:
                session_deleted = True
            else:
                session_status = current.status
        task_status = None
        result_status = None
        observed_at: datetime | None = None
        if record.task_id is not None:
            task = await repository.task(record.task_id)
            if task is not None:
                task_status = task.status
                observed_at = task.updated_at
            result = await repository.task_result(record.task_id)
            if result is not None:
                result_status = result.status
                observed_at = result.finished_at or result.created_at
        observation: dict[str, object] = {
            "task_status": task_status,
            "session_status": session_status,
            "result_status": result_status,
            "observed_at": observed_at or datetime.now(UTC),
            "session_deleted": session_deleted,
        }
        return SessionCreateObservation(receipt=record.receipt_json, observation=observation)

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
        request_id: UUID | None = None,
        schema_version: int | None = None,
        argv_digest: str | None = None,
        intent_fingerprint: str | None = None,
        sync_target: dict[str, object] | None = None,
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
        :param request_id (UUID | None): 调用方生成的原始请求键
        :param schema_version (int | None): 调用方请求协议版本
        :param argv_digest (str | None): 原始命令参数摘要
        :param intent_fingerprint (str | None): 完整创建意图摘要
        :param sync_target (dict[str, object] | None): 调用方确认的同步目标
        :return Session: 完整受理的启动会话
        """
        create_requests = SessionCreateRequestRepository(self._session)
        caller = self._caller_intent(
            request_id=request_id,
            schema_version=schema_version,
            tool_type=tool_type,
            tool_account_id=tool_account_id,
            workspace_id=workspace_id,
            project_key=project_key,
            argv=argv,
            replaces_session_id=replaces_session_id,
            argv_digest=argv_digest,
            intent_fingerprint=intent_fingerprint,
            sync_target=sync_target,
        )
        if caller is not None:
            prior = await create_requests.get(user.id, caller.request_id)
            if prior is not None:
                return await self._replay_create_request(user, prior, caller.intent_fingerprint)
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
                if caller is not None and isinstance(result, Session):
                    task = await self._repository.get_task_by_task_id(
                        f"create_tool_session:{result.id}"
                    )
                    receipt = self._receipt_for_session(caller=caller, session=result, task=task)
                    await create_requests.add(
                        SessionCreateRequest(
                            user_id=user.id,
                            request_id=caller.request_id,
                            schema_version=caller.schema_version,
                            tool_type=tool_type,
                            tool_account_id=tool_account_id,
                            workspace_id=workspace_id,
                            project_key=project_key,
                            argv_digest=caller.argv_digest,
                            intent_fingerprint=caller.intent_fingerprint,
                            replacement_session_id=replaces_session_id,
                            sync_session_id=caller.sync_session_id,
                            node_id=result.node_id,
                            session_id=result.id,
                            task_record_id=task.id if task is not None else None,
                            task_id=task.task_id if task is not None else None,
                            phase="accepted",
                            receipt_json=receipt,
                            sync_target_json=sync_target,
                            observation_json={"session_status": result.status},
                        )
                    )
                elif caller is not None:
                    pending_code = (
                        "MIGRATION_PENDING"
                        if isinstance(result, SkillTakeoverPending)
                        else "STATE_MIGRATION_REQUIRED"
                    )
                    pending_phase = (
                        "admission_pending"
                        if isinstance(result, SkillTakeoverPending)
                        else "migration_pending"
                    )
                    pending_receipt: dict[str, object] = {
                        "schema_version": 1,
                        "request_id": str(caller.request_id),
                        "tool_type": tool_type,
                        "tool_account_id": str(tool_account_id),
                        "workspace_id": str(workspace_id),
                        "project_key": project_key,
                        "argv_digest": caller.argv_digest,
                        "replacement_session_id": str(replaces_session_id)
                        if replaces_session_id is not None
                        else None,
                        "sync_target_digest": (
                            caller.sync_target.get("target_digest")
                            if caller.sync_target is not None
                            else None
                        ),
                        "phase": pending_phase,
                        "session_id": None,
                        "create_task_id": None,
                        "task_record_id": None,
                        "error_code": pending_code,
                    }
                    await create_requests.add(
                        SessionCreateRequest(
                            user_id=user.id,
                            request_id=caller.request_id,
                            schema_version=caller.schema_version,
                            tool_type=tool_type,
                            tool_account_id=tool_account_id,
                            workspace_id=workspace_id,
                            project_key=project_key,
                            argv_digest=caller.argv_digest,
                            intent_fingerprint=caller.intent_fingerprint,
                            replacement_session_id=replaces_session_id,
                            sync_session_id=caller.sync_session_id,
                            phase=pending_phase,
                            acceptance_code=pending_code,
                            receipt_json=pending_receipt,
                            sync_target_json=sync_target,
                            observation_json={},
                        )
                    )
        except SkillContentError as error:
            status = 413 if error.code in {"QUOTA_EXCEEDED", "CONTENT_TOO_LARGE"} else 409
            raise ApiError(
                code=error.code, message=str(error), status_code=status, details=error.details
            ) from error
        except IntegrityError:
            await self._session.rollback()
            if caller is not None:
                prior = await create_requests.get(user.id, caller.request_id)
                if prior is not None:
                    return await self._replay_create_request(user, prior, caller.intent_fingerprint)
            raise
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

    async def _stop_session_locked(
        self, *, user: User, tool_session: Session
    ) -> StopSessionLockedResult:
        """
        在调用方已经取得历史锁和会话行锁后安排一次停止。

        :param user (User): 当前用户
        :param tool_session (Session): 已锁定工具会话
        :return StopSessionLockedResult: 会话、设备撤销结果和浏览器服务
        """
        # Stop intent ends the exact attachment lease before the Node task is
        # delivered. This closes the local transfer gate immediately while the
        # persisted session status still converges through the normal task.
        await SessionAttachmentService(self._session).invalidate_for_session(
            tool_session.id, "session_stopping"
        )
        device_stop = None
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
        else:
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
        return tool_session, device_stop, ego_service

    async def stop_session(self, *, user: User, session_id: UUID) -> Session:
        """
        停止工具运行 session

        :param user (User): 当前用户
        :param session_id (UUID): 工具 session ID
        :return Session: 工具 session 实体
        """

        async with existing_history_mutations(self._session, (user.id,)):
            tool_session = await self._require_locked_user_session(user=user, session_id=session_id)
            _, device_stop, ego_service = await self._stop_session_locked(
                user=user, tool_session=tool_session
            )
        await self._session.commit()
        if device_stop is not None:
            await DeviceSessionService(
                self._session, self._settings, self._relay_hub
            ).close_revoked_bindings(device_stop.revoked_bindings)
        await ego_service.publish_pending_revocations()
        return tool_session

    def _stop_intent_fingerprint(
        self,
        *,
        session_id: UUID,
        node_id: UUID,
        runtime_backend: str,
        runtime_resource_id: str | None,
        reason: str,
        supplied: str | None,
    ) -> str:
        """
        计算并校验不含凭据的停止目标摘要。

        :param session_id (UUID): 原始会话身份
        :param node_id (UUID): 原始节点身份
        :param runtime_backend (str): 运行时后端
        :param runtime_resource_id (str | None): 运行时资源身份
        :param reason (str): 调用方停止原因
        :param supplied (str | None): 调用方提交摘要
        :return str: 稳定停止目标摘要
        :raises ApiError: 调用方摘要与当前目标不一致
        """
        computed = _digest(
            {
                "schema_version": 1,
                "session_id": str(session_id),
                "node_id": str(node_id),
                "runtime_backend": runtime_backend,
                "runtime_resource_id": runtime_resource_id,
                "reason": reason,
            }
        )
        if supplied is not None and supplied != computed:
            if not _valid_digest(supplied):
                raise ApiError(
                    code="SESSION_STOP_INTENT_DIGEST_INVALID",
                    message="Session stop intent digest is invalid.",
                    status_code=422,
                )
            raise ApiError(
                code="SESSION_STOP_INTENT_MISMATCH",
                message="Session stop intent does not match the target.",
                status_code=409,
            )
        return computed

    async def get_stop_request(self, *, user: User, request_id: UUID) -> SessionStopObservation:
        """
        按所有者和原键读取停止不可变收据及当前观察。

        :param user (User): 当前用户
        :param request_id (UUID): 调用方原始停止请求标识
        :return SessionStopObservation: 停止收据与只读观察
        :raises ApiError: 原键不存在
        """
        repository = SessionStopRequestRepository(self._session)
        record = await repository.get(user.id, request_id)
        if record is None:
            raise ApiError(
                code="SESSION_STOP_REQUEST_NOT_FOUND",
                message="Session stop request was not found.",
                status_code=404,
            )
        current = await repository.get_session(record.session_id)
        task = await repository.task(record.task_id) if record.task_id is not None else None
        result = (
            await repository.task_result(record.task_id) if record.task_id is not None else None
        )
        session_deleted = current is None
        session_status = current.status if current is not None else None
        result_status = result.status if result is not None else None
        process_stopped = None
        if result is not None and result.status == "succeeded" and isinstance(result.result, dict):
            process_stopped = result.result.get("status") == "stopped"
        if session_deleted:
            phase = "deleted"
        elif process_stopped is True or session_status == "stopped":
            phase = "stopped"
        elif (
            session_status == "failed"
            or task is not None
            and task.status in {"failed", "cancelled", "expired"}
            or result is not None
            and result.status == "failed"
        ):
            phase = "failed"
        elif session_status == "stopping":
            phase = "stopping"
        else:
            phase = "accepted"
        snapshot = await SkillRuntimeRepository(self._session).snapshot_for_session(
            user.id, record.session_id
        )
        save_status = None
        if snapshot is not None:
            try:
                save_status = (
                    await SkillStopStatusService(self._session).read(user.id, snapshot.id)
                ).status
            except SkillContentError:
                save_status = None
        observed_at = record.updated_at
        for value in (
            task.updated_at if task is not None else None,
            result.finished_at if result is not None else None,
            result.created_at if result is not None else None,
        ):
            if value is not None and value > observed_at:
                observed_at = value
        observation: dict[str, object] = {
            "stop_task_status": task.status if task is not None else None,
            "session_status": session_status,
            "result_status": result_status,
            "skill_finalization_operation_id": str(snapshot.id) if snapshot is not None else None,
            "session_deleted": session_deleted,
            "process_stopped": process_stopped,
            "save_status": save_status,
            "phase": phase,
            "observed_at": observed_at,
        }
        return SessionStopObservation(receipt=record.receipt_json, observation=observation)

    async def stop_session_request(
        self,
        *,
        user: User,
        session_id: UUID,
        request_id: UUID,
        schema_version: int,
        reason: str,
        intent_fingerprint: str | None,
        expected_session_id: UUID | None = None,
        expected_node_id: UUID | None = None,
        expected_runtime_backend: str | None = None,
        expected_stop_task_id: str | None = None,
    ) -> SessionStopObservation:
        """
        原子受理一次准确停止并持久化原任务身份。

        :param user (User): 当前用户
        :param session_id (UUID): 原始会话身份
        :param request_id (UUID): 调用方生成的原始键
        :param schema_version (int): 调用方协议版本
        :param reason (str): 不含敏感内容的停止原因
        :param intent_fingerprint (str | None): 调用方目标摘要
        :param expected_session_id (UUID | None): 调用方确认的会话身份
        :param expected_node_id (UUID | None): 调用方确认的节点身份
        :param expected_runtime_backend (str | None): 调用方确认的运行时后端
        :param expected_stop_task_id (str | None): 调用方确认的已有停止任务
        :return SessionStopObservation: 不可变收据与当前观察
        :raises ApiError: 版本、原键或会话状态不满足合同
        """
        if schema_version != 1:
            raise ApiError(
                code="SESSION_STOP_SCHEMA_UNSUPPORTED",
                message="Session stop request schema is unsupported.",
                status_code=422,
            )
        if not reason or len(reason) > 64:
            raise ApiError(
                code="SESSION_STOP_REASON_INVALID",
                message="Session stop reason is invalid.",
                status_code=422,
            )
        repository = SessionStopRequestRepository(self._session)
        device_stop = None
        ego_service: EgoBrowserService | None = None
        async with existing_history_mutations(self._session, (user.id,)):
            prior = await repository.get_for_update(user.id, request_id)
            if prior is not None:
                if (
                    prior.session_id != session_id
                    or expected_session_id is not None
                    and prior.session_id != expected_session_id
                    or expected_node_id is not None
                    and prior.node_id != expected_node_id
                    or expected_runtime_backend is not None
                    and prior.runtime_backend != expected_runtime_backend
                    or expected_stop_task_id is not None
                    and prior.task_id != expected_stop_task_id
                ):
                    raise ApiError(
                        code="SESSION_STOP_REQUEST_CONFLICT",
                        message="Request identity is already bound to a different session.",
                        status_code=409,
                    )
                if prior.reason != reason or (
                    intent_fingerprint is not None
                    and prior.intent_fingerprint != intent_fingerprint
                ):
                    raise ApiError(
                        code="SESSION_STOP_REQUEST_CONFLICT",
                        message="Request identity is already bound to a different stop.",
                        status_code=409,
                    )
            else:
                tool_session = await self._require_locked_user_session(
                    user=user, session_id=session_id
                )
                if (
                    expected_session_id is not None
                    and expected_session_id != tool_session.id
                    or expected_node_id is not None
                    and expected_node_id != tool_session.node_id
                    or expected_runtime_backend is not None
                    and expected_runtime_backend != tool_session.runtime_backend
                ):
                    raise ApiError(
                        code="SESSION_STOP_BINDING_MISMATCH",
                        message="Session stop binding does not match the current session.",
                        status_code=409,
                    )
                existing_task = await self._repository.get_task_by_task_id(
                    f"stop_tool_session:{tool_session.id}"
                )
                if expected_stop_task_id is not None and (
                    existing_task is None or existing_task.task_id != expected_stop_task_id
                ):
                    raise ApiError(
                        code="SESSION_STOP_BINDING_MISMATCH",
                        message="Session stop task does not match the current session.",
                        status_code=409,
                    )
                fingerprint = self._stop_intent_fingerprint(
                    session_id=tool_session.id,
                    node_id=tool_session.node_id,
                    runtime_backend=tool_session.runtime_backend,
                    runtime_resource_id=tool_session.runtime_resource_id,
                    reason=reason,
                    supplied=intent_fingerprint,
                )
                tool_session, device_stop, ego_service = await self._stop_session_locked(
                    user=user, tool_session=tool_session
                )
                task = await self._repository.get_task_by_task_id(
                    f"stop_tool_session:{tool_session.id}"
                )
                task_failed = task is not None and task.status in {
                    "failed",
                    "cancelled",
                    "expired",
                }
                unresolved = tool_session.status == "failed" or task_failed
                if tool_session.status == "stopped":
                    phase = "stopped"
                elif unresolved:
                    phase = "failed"
                else:
                    phase = "accepted"
                acceptance_code = "SESSION_STOP_UNRESOLVED" if unresolved else None
                snapshot = await SkillRuntimeRepository(self._session).snapshot_for_session(
                    user.id, tool_session.id
                )
                receipt: dict[str, object] = {
                    "schema_version": 1,
                    "request_id": str(request_id),
                    "session_id": str(tool_session.id),
                    "node_id": str(tool_session.node_id),
                    "runtime_backend": tool_session.runtime_backend,
                    "runtime_resource_id": tool_session.runtime_resource_id,
                    "stop_task_id": task.task_id if task is not None else None,
                    "stop_task_record_id": str(task.id) if task is not None else None,
                    "skill_finalization_operation_id": str(snapshot.id)
                    if snapshot is not None
                    else None,
                    "phase": phase,
                    "error_code": acceptance_code,
                    "intent_fingerprint": fingerprint,
                    "reason": reason,
                }
                try:
                    await repository.add(
                        SessionStopRequest(
                            user_id=user.id,
                            request_id=request_id,
                            schema_version=1,
                            session_id=tool_session.id,
                            node_id=tool_session.node_id,
                            runtime_backend=tool_session.runtime_backend,
                            runtime_resource_id=tool_session.runtime_resource_id,
                            task_record_id=task.id if task is not None else None,
                            task_id=task.task_id if task is not None else None,
                            intent_fingerprint=fingerprint,
                            reason=reason,
                            phase=phase,
                            acceptance_code=acceptance_code,
                            receipt_json=receipt,
                            observation_json={},
                        )
                    )
                except IntegrityError:
                    await self._session.rollback()
                    prior = await repository.get(user.id, request_id)
                    if prior is not None and prior.session_id == session_id:
                        return await self.get_stop_request(user=user, request_id=request_id)
                    raise
        await self._session.commit()
        if device_stop is not None:
            await DeviceSessionService(
                self._session, self._settings, self._relay_hub
            ).close_revoked_bindings(device_stop.revoked_bindings)
        if ego_service is not None:
            await ego_service.publish_pending_revocations()
        return await self.get_stop_request(user=user, request_id=request_id)

    def _delete_selection_fingerprints(
        self,
        *,
        schema_version: int,
        tool_type: str,
        session_ids: list[UUID],
        selection: dict[str, object],
        reason: str,
        selection_digest: str,
        intent_fingerprint: str,
    ) -> tuple[str, str, list[str], dict[str, object]]:
        """
        规范化并校验固定集合删除的两个摘要。

        :param schema_version (int): 删除协议版本
        :param tool_type (str): 工具类型
        :param session_ids (list[UUID]): 用户确认的会话集合
        :param selection (dict[str, object]): 用户确认的筛选摘要
        :param reason (str): 删除原因
        :param selection_digest (str): 外部集合摘要
        :param intent_fingerprint (str): 外部完整意图摘要
        :return tuple[str, str, list[str], dict[str, object]]: 集合、意图、规范 ID 和筛选摘要
        :raises ApiError: 摘要、集合或筛选字段不合法
        """
        if schema_version != 1:
            raise ApiError(
                code="SESSION_DELETE_SCHEMA_UNSUPPORTED",
                message="Session delete request schema is unsupported.",
                status_code=422,
            )
        if reason != "user_requested":
            raise ApiError(
                code="SESSION_DELETE_REASON_INVALID",
                message="Session delete reason is invalid.",
                status_code=422,
            )
        if not session_ids or len(session_ids) > 256 or len(set(session_ids)) != len(session_ids):
            raise ApiError(
                code="SESSION_DELETE_SESSION_SET_INVALID",
                message="Session delete IDs must be a non-empty unique set.",
                status_code=422,
            )
        if not isinstance(selection, dict) or selection.get("tool_type") != tool_type:
            raise ApiError(
                code="SESSION_DELETE_SELECTION_INVALID",
                message="Session delete selection does not match the tool type.",
                status_code=422,
            )
        statuses_value = selection.get("statuses")
        if not isinstance(statuses_value, list):
            raise ApiError(
                code="SESSION_DELETE_SELECTION_INVALID",
                message="Session delete selection statuses are invalid.",
                status_code=422,
            )
        statuses = sorted(set(str(value) for value in statuses_value))
        if not statuses or set(statuses) - DELETABLE_SESSION_STATUSES:
            raise ApiError(
                code="SESSION_DELETE_SELECTION_INVALID",
                message="Session delete selection contains a protected status.",
                status_code=422,
            )
        project_key = selection.get("project_key")
        if project_key is not None and (
            not isinstance(project_key, str) or not project_key or len(project_key) > 256
        ):
            raise ApiError(
                code="SESSION_DELETE_SELECTION_INVALID",
                message="Session delete project selection is invalid.",
                status_code=422,
            )
        normalized_selection: dict[str, object] = {
            "tool_type": tool_type,
            "statuses": statuses,
            "project_key": project_key,
        }
        normalized_ids = sorted(str(value) for value in session_ids)
        canonical = {
            "schema_version": 1,
            "tool_type": tool_type,
            "session_ids": normalized_ids,
            "selection": normalized_selection,
        }
        computed_selection = _digest(canonical)
        if not _valid_digest(selection_digest):
            raise ApiError(
                code="SESSION_DELETE_SELECTION_DIGEST_INVALID",
                message="Session delete selection digest is invalid.",
                status_code=422,
            )
        if selection_digest != computed_selection:
            raise ApiError(
                code="SESSION_DELETE_SELECTION_DIGEST_MISMATCH",
                message="Session delete selection digest does not match the confirmed set.",
                status_code=409,
            )
        computed_intent = _digest({**canonical, "reason": reason})
        if not _valid_digest(intent_fingerprint):
            raise ApiError(
                code="SESSION_DELETE_INTENT_DIGEST_INVALID",
                message="Session delete intent fingerprint is invalid.",
                status_code=422,
            )
        if intent_fingerprint != computed_intent:
            raise ApiError(
                code="SESSION_DELETE_INTENT_MISMATCH",
                message="Session delete intent does not match the confirmed set.",
                status_code=409,
            )
        return computed_selection, computed_intent, normalized_ids, normalized_selection

    async def get_delete_request(self, *, user: User, request_id: UUID) -> SessionDeleteObservation:
        """
        按所有者和原键读取固定集合删除不可变回执及当前观察。

        :param user (User): 当前用户
        :param request_id (UUID): 调用方原始删除请求标识
        :return SessionDeleteObservation: 删除回执与只读观察
        :raises ApiError: 原键不存在
        """
        repository = SessionDeleteRequestRepository(self._session)
        record = await repository.get(user.id, request_id)
        if record is None:
            raise ApiError(
                code="SESSION_DELETE_REQUEST_NOT_FOUND",
                message="Session delete request was not found.",
                status_code=404,
            )
        raw_ids = record.session_ids
        session_ids = [UUID(str(value)) for value in raw_ids]
        current = (
            await self._session.scalars(
                select(Session).where(Session.user_id == user.id, Session.id.in_(session_ids))
            )
        ).all()
        current_ids = {str(value.id) for value in current}
        deleted_ids = [value for value in raw_ids if value not in current_ids]
        receipt = dict(record.receipt_json)
        observation = dict(record.observation_json)
        observation.update(
            {
                "phase": record.phase,
                "task_status": "succeeded" if record.phase == "deleted" else None,
                "deleted_session_ids": deleted_ids,
                "session_deleted": len(deleted_ids) == len(raw_ids),
                "collection_mismatch": False,
                "observed_at": record.updated_at,
            }
        )
        return SessionDeleteObservation(receipt=receipt, observation=observation)

    async def delete_session_collection_request(
        self,
        *,
        user: User,
        request_id: UUID,
        schema_version: int,
        tool_type: str,
        session_ids: list[UUID],
        selection: dict[str, object],
        selection_digest: str,
        intent_fingerprint: str,
        reason: str,
    ) -> SessionDeleteObservation:
        """
        原子删除用户确认的固定会话集合并保留不可变原键回执。

        :param user (User): 当前用户
        :param request_id (UUID): 调用方原始删除请求标识
        :param schema_version (int): 删除协议版本
        :param tool_type (str): 工具类型
        :param session_ids (list[UUID]): 用户确认的完整会话集合
        :param selection (dict[str, object]): 用户确认的筛选摘要
        :param selection_digest (str): 集合选择摘要
        :param intent_fingerprint (str): 完整删除意图摘要
        :param reason (str): 删除原因
        :return SessionDeleteObservation: 不可变回执与当前观察
        :raises ApiError: 集合变化、受保护状态或 Skill 历史未解决
        """
        computed_selection, computed_intent, normalized_ids, normalized_selection = (
            self._delete_selection_fingerprints(
                schema_version=schema_version,
                tool_type=tool_type,
                session_ids=session_ids,
                selection=selection,
                reason=reason,
                selection_digest=selection_digest,
                intent_fingerprint=intent_fingerprint,
            )
        )
        repository = SessionDeleteRequestRepository(self._session)
        revoked_bindings: list[RevokedDeviceBinding] = []
        ego_service: EgoBrowserService | None = None
        async with existing_history_mutations(self._session, (user.id,)):
            prior = await repository.get_for_update(user.id, request_id)
            if prior is not None:
                if (
                    prior.tool_type != tool_type
                    or prior.session_ids != normalized_ids
                    or prior.selection_digest != computed_selection
                    or prior.intent_fingerprint != computed_intent
                    or prior.reason != reason
                ):
                    raise ApiError(
                        code="SESSION_DELETE_REQUEST_CONFLICT",
                        message=(
                            "Request identity is already bound to a different collection delete."
                        ),
                        status_code=409,
                    )
                return await self.get_delete_request(user=user, request_id=request_id)

            raw_statuses = normalized_selection["statuses"]
            if not isinstance(raw_statuses, list):
                raise RuntimeError("normalized session delete statuses are not a list")
            statuses = {str(value) for value in raw_statuses}
            project_key = normalized_selection["project_key"]
            current = list(
                await self._repository.list_sessions_for_collection(
                    user_id=user.id,
                    tool_type=tool_type,
                    statuses=statuses,
                    project_key=project_key if isinstance(project_key, str) else None,
                )
            )
            current_ids = sorted(str(value.id) for value in current)
            if current_ids != normalized_ids:
                raise ApiError(
                    code="SESSION_DELETE_COLLECTION_MISMATCH",
                    message="The confirmed session collection changed before deletion.",
                    status_code=409,
                    details={"collection_mismatch": True},
                )
            if any(value.user_id != user.id for value in current):
                raise ApiError(
                    code="SESSION_DELETE_CONTEXT_MISMATCH",
                    message="A selected session owner does not match the authenticated user.",
                    status_code=409,
                    details={"collection_mismatch": True},
                )
            status_snapshot = {str(value.id): value.status for value in current}
            context_snapshot = [
                {
                    "session_id": str(value.id),
                    "tool_account_id": str(value.tool_account_id),
                    "workspace_id": str(value.workspace_id),
                    "project_key": value.project_key,
                    "node_id": str(value.node_id),
                    "runtime_backend": value.runtime_backend,
                    "runtime_resource_id": value.runtime_resource_id,
                }
                for value in current
            ]
            for tool_session in current:
                await require_settled_session_stops(self._session, tool_session)
            ego_service = EgoBrowserService(
                self._session,
                self._settings,
                revocation_publisher=self._ego_browser_revocation_publisher,
            )
            for tool_session in current:
                await release_retained_session_reference(self._session, tool_session)
                await SessionAttachmentService(self._session).invalidate_for_session(
                    tool_session.id, "session_deleted"
                )
                device_stop = await DeviceSessionService(
                    self._session, self._settings, self._relay_hub
                ).stop_for_tool_session(
                    tool_session_id=tool_session.id,
                    reason="tool_session_collection_delete",
                    actor_user_id=user.id,
                    audit_action="device_session.session_stop",
                    commit=False,
                )
                revoked_bindings.extend(device_stop.revoked_bindings)
                await ego_service.revoke_for_tool_session(
                    tool_session_id=tool_session.id,
                    reason="tool_session_collection_delete",
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
                    details={"status": tool_session.status, "request_id": str(request_id)},
                )
                await self._repository.delete_session(tool_session)
            receipt: dict[str, object] = {
                "schema_version": 1,
                "request_id": str(request_id),
                "tool_type": tool_type,
                "session_ids": normalized_ids,
                "selection": normalized_selection,
                "selection_digest": computed_selection,
                "intent_fingerprint": computed_intent,
                "reason": reason,
                "phase": "deleted",
                "delete_task_id": None,
                "delete_task_record_id": None,
                "task_status": "succeeded",
                "deleted_session_ids": normalized_ids,
                "session_statuses": status_snapshot,
                "session_contexts": context_snapshot,
                "session_deleted": True,
                "collection_mismatch": False,
                "error_code": None,
            }
            ledger_id = uuid4()
            receipt["delete_task_record_id"] = str(ledger_id)
            record = await repository.add(
                SessionDeleteRequest(
                    id=ledger_id,
                    user_id=user.id,
                    request_id=request_id,
                    schema_version=1,
                    tool_type=tool_type,
                    session_ids=normalized_ids,
                    selection_summary=normalized_selection,
                    selection_digest=computed_selection,
                    intent_fingerprint=computed_intent,
                    reason=reason,
                    phase="deleted",
                    acceptance_code=None,
                    receipt_json=receipt,
                    observation_json={
                        "phase": "deleted",
                        "task_status": "succeeded",
                        "deleted_session_ids": normalized_ids,
                        "session_contexts": context_snapshot,
                        "session_deleted": True,
                        "collection_mismatch": False,
                    },
                )
            )
            record.receipt_json = receipt
            await self._session.flush()
            await self._audit(
                actor_user_id=user.id,
                action="sessions.collection_delete",
                target_type="session_collection",
                target_id=str(request_id),
                details={
                    "request_id": str(request_id),
                    "tool_type": tool_type,
                    "session_ids": normalized_ids,
                    "selection_digest": computed_selection,
                    "deleted_count": len(normalized_ids),
                },
            )
        await self._session.commit()
        await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).close_revoked_bindings(revoked_bindings)
        if ego_service is not None:
            await ego_service.publish_pending_revocations()
        return await self.get_delete_request(user=user, request_id=request_id)

    async def delete_session(self, *, user: User, session_id: UUID) -> None:
        """
        删除当前用户已停止、已中断或失败的工具 session

        :param user (User): 当前用户
        :param session_id (UUID): 工具 session ID
        :raises ApiError: session 不属于当前用户或尚未进入可删除状态
        """

        async with existing_history_mutations(self._session, (user.id,)):
            tool_session = await self._require_locked_user_session(user=user, session_id=session_id)
            if tool_session.status not in DELETABLE_SESSION_STATUSES:
                raise ApiError(
                    code="SESSION_DELETE_NOT_ALLOWED",
                    message="Only stopped, interrupted, or failed sessions can be deleted.",
                    status_code=409,
                )
            await require_settled_session_stops(self._session, tool_session)
            await release_retained_session_reference(self._session, tool_session)
            await SessionAttachmentService(self._session).invalidate_for_session(
                tool_session.id, "session_deleted"
            )
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

        async with existing_history_mutations(self._session, (user.id,)):
            sessions = list(
                await self._repository.list_sessions_for_user_by_statuses(
                    user.id, DELETABLE_SESSION_STATUSES
                )
            )
            if not sessions:
                return 0
            for tool_session in sessions:
                await require_settled_session_stops(self._session, tool_session)
            for tool_session in sessions:
                await release_retained_session_reference(self._session, tool_session)
            revoked_bindings: list[RevokedDeviceBinding] = []
            ego_service = EgoBrowserService(
                self._session,
                self._settings,
                revocation_publisher=self._ego_browser_revocation_publisher,
            )
            for tool_session in sessions:
                await SessionAttachmentService(self._session).invalidate_for_session(
                    tool_session.id, "session_deleted"
                )
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

    async def _require_locked_user_session(self, *, user: User, session_id: UUID) -> Session:
        """
        生命周期变更必须在等待原会话锁后重新校验所属关系和状态。

        :param user (User): 已认证用户
        :param session_id (UUID): 原会话身份
        :return Session: 已锁定并刷新的会话
        """
        tool_session = await self._repository.lock_session(session_id, user.id)
        if tool_session is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Session was not found.", status_code=404
            )
        return tool_session

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
