"""
实现节点业务逻辑。
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.ego_browser.relay import EgoBrowserRevocationPublisher
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    BrowserSession,
    Node,
    NodeHeartbeat,
    NodeJoinCode,
    NodeTask,
    NodeTaskResult,
    Session,
    SyncSession,
    ToolAccount,
    ToolAccountProfile,
    User,
)
from agent_remote_server.repositories import NodeRepository
from agent_remote_server.repositories.ego_browser import EgoBrowserRepository
from agent_remote_server.repositories.identity import IdentityRepository
from agent_remote_server.security import create_opaque_token, decrypt_text, encrypt_text, hash_token
from agent_remote_server.services.device_sessions import (
    DeviceSessionService,
    RevokedDeviceBinding,
)
from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.ego_browser.helpers import _as_utc
from agent_remote_server.services.port_forward_revocation import revoke_port_forwards

RUNTIME_BACKENDS = {"docker_sandbox", "native"}
_EGO_BROWSER_CANCEL_RESULT_KEYS = {
    "status",
    "request_active",
    "server_terminal_observed",
}


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


@dataclass(frozen=True)
class NodeJoinCodeIssueResult:
    """
    Node 加入码签发结果；原文只在签发瞬间返回。
    """

    node: Node
    raw_code: str
    expires_at: datetime
    ego_browser_enabled: bool | None
    server_origin: str
    release_profile: str
    wrapper_version: str
    skill_version: str
    runtime_version: str | None
    artifact_digest: str
    profile_digest: str


@dataclass(frozen=True)
class NodeJoinCodeExchangeResult:
    """
    Node 加入码交换结果。
    """

    node: Node
    raw_node_token: str
    exchange_id: str
    server_origin: str
    release_profile: str
    wrapper_version: str
    skill_version: str
    runtime_version: str | None
    artifact_digest: str
    profile_digest: str
    ego_browser_enabled_intent: bool | None


@dataclass(frozen=True)
class NodeJoinProfile:
    """
    Node 加入码绑定的非秘密发布元数据。
    """

    server_origin: str
    release_profile: str
    wrapper_version: str
    skill_version: str
    runtime_version: str | None
    artifact_digest: str
    profile_digest: str


class NodeService:
    """
    节点管理和节点任务服务
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_hub: DeviceRelayHub | None = None,
        ego_browser_revocation_publisher: EgoBrowserRevocationPublisher | None = None,
    ) -> None:
        """
        初始化节点业务服务。

        :param session (AsyncSession): 会话
        :param settings (Settings): 配置
        :param relay_hub (DeviceRelayHub | None): 中继中心
        :param ego_browser_revocation_publisher (EgoBrowserRevocationPublisher | None): 撤销发布器
        """
        self._session = session
        self._settings = settings
        self._repository = NodeRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._relay_hub = relay_hub
        self._ego_browser_revocation_publisher = ego_browser_revocation_publisher

    def _require_enrollment(self) -> None:
        """
        检查 Node 加入流程是否被 Server enrollment 闸门允许。
        """

        if not self._settings.ego_browser_enrollment_enabled:
            raise ApiError(
                code="EGO_BROWSER_ENROLLMENT_DISABLED",
                message="Ego-browser enrollment is disabled.",
                status_code=503,
            )

    def _join_profile(self, node: Node, *, ego_browser_enabled: bool | None) -> NodeJoinProfile:
        """
        从 Server 发布策略和已验证能力生成加入码 profile。

        :param node (Node): 节点
        :param ego_browser_enabled (bool | None): Ego Browser 启用状态
        :return NodeJoinProfile: 拼接配置
        """

        raw_bridge = node.runtime_capabilities.get("ego_browser_bridge")
        bridge = raw_bridge if isinstance(raw_bridge, dict) else {}
        wrapper_version = self._settings.ego_browser_expected_wrapper_version
        skill_version = self._settings.ego_browser_expected_skill_version
        artifact_digest = f"sha256:{self._settings.ego_browser_expected_skill_tree_sha256}"
        reported_wrapper = bridge.get("wrapper_version")
        reported_skill = bridge.get("skill_version")
        reported_digest = bridge.get("skill_tree_sha256")
        if reported_wrapper is not None and reported_wrapper != wrapper_version:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="The node wrapper does not match the approved join profile.",
                status_code=409,
            )
        if reported_skill is not None and reported_skill != skill_version:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="The node Skill does not match the approved join profile.",
                status_code=409,
            )
        if reported_digest is not None and f"sha256:{reported_digest}" != artifact_digest:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="The node artifact does not match the approved join profile.",
                status_code=409,
            )
        server_origin = self._settings.public_origin
        release_profile = self._settings.ego_browser_expected_release_profile
        runtime_version = node.version
        intent = (
            "preserve"
            if ego_browser_enabled is None
            else "true"
            if ego_browser_enabled
            else "false"
        )
        profile_material = "\0".join(
            (
                "node-join-profile-v1",
                server_origin,
                str(node.id),
                release_profile,
                wrapper_version,
                skill_version,
                runtime_version or "",
                artifact_digest,
                intent,
            )
        )
        profile_digest = hashlib.sha256(profile_material.encode()).hexdigest()
        return NodeJoinProfile(
            server_origin=server_origin,
            release_profile=release_profile,
            wrapper_version=wrapper_version,
            skill_version=skill_version,
            runtime_version=runtime_version,
            artifact_digest=artifact_digest,
            profile_digest=profile_digest,
        )

    @staticmethod
    def _join_profile_digest(
        *,
        server_origin: str,
        node_id: UUID,
        release_profile: str,
        wrapper_version: str,
        skill_version: str,
        runtime_version: str | None,
        artifact_digest: str,
        ego_browser_enabled: bool | None,
    ) -> str:
        """
        返回拼接配置摘要。

        :param server_origin (str): 服务端来源
        :param node_id (UUID): 节点 ID
        :param release_profile (str): 发布配置
        :param wrapper_version (str): wrapper 版本
        :param skill_version (str): skill 版本
        :param runtime_version (str | None): 运行时版本
        :param artifact_digest (str): 制品摘要
        :param ego_browser_enabled (bool | None): Ego Browser 启用状态
        :return str: 拼接配置摘要
        """

        intent = (
            "preserve"
            if ego_browser_enabled is None
            else "true"
            if ego_browser_enabled
            else "false"
        )
        material = "\0".join(
            (
                "node-join-profile-v1",
                server_origin,
                str(node_id),
                release_profile,
                wrapper_version,
                skill_version,
                runtime_version or "",
                artifact_digest,
                intent,
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _join_profile_is_complete(record: NodeJoinCode) -> bool:
        """
        判断加入码发布配置是否完整。

        :param record (NodeJoinCode): 记录
        :return bool: 是否满足校验条件
        """

        return all(
            isinstance(value, str) and bool(value)
            for value in (
                record.server_origin,
                record.release_profile,
                record.wrapper_version,
                record.skill_version,
                record.artifact_digest,
                record.profile_digest,
            )
        )

    @staticmethod
    def _valid_join_exchange_id(value: str) -> bool:
        """
        校验跨控制工作站和 Node 持久化的交换标识。

        :param value (str): 值
        :return bool: 是否满足校验条件
        """

        return 16 <= len(value) <= 128 and all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )

    async def _validate_join_exchange_record(
        self,
        *,
        record: NodeJoinCode,
        node_id: UUID | None,
        expected_code_hash: str | None,
        release_profile: str | None,
        wrapper_version: str | None,
        skill_version: str | None,
        runtime_version: str | None,
        artifact_digest: str | None,
        profile_digest: str | None,
        ego_browser_enabled: bool | None,
        require_profile_echo: bool,
    ) -> Node:
        """
        校验拼接交换记录。

        :param record (NodeJoinCode): 记录
        :param node_id (UUID | None): 节点 ID
        :param expected_code_hash (str | None): 预期代码 hash
        :param release_profile (str | None): 发布配置
        :param wrapper_version (str | None): wrapper 版本
        :param skill_version (str | None): skill 版本
        :param runtime_version (str | None): 运行时版本
        :param artifact_digest (str | None): 制品摘要
        :param profile_digest (str | None): 配置摘要
        :param ego_browser_enabled (bool | None): Ego Browser 启用状态
        :param require_profile_echo (bool): 获取并校验配置 echo
        :return Node: 拼接交换记录
        """

        if expected_code_hash is not None and record.code_hash != expected_code_hash:
            raise ApiError(
                code="NODE_JOIN_CODE_EXCHANGE_CONFLICT",
                message="The exchange ID is already bound to another join code.",
                status_code=409,
            )
        if not self._join_profile_is_complete(record):
            # 发布配置是授权边界；旧记录或损坏记录必须显式修复，不能返回空元数据。
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_INCOMPLETE",
                message="The join-code release profile is incomplete.",
                status_code=409,
            )
        release_profile_value = record.release_profile
        wrapper_version_value = record.wrapper_version
        skill_version_value = record.skill_version
        artifact_digest_value = record.artifact_digest
        profile_digest_value = record.profile_digest
        assert release_profile_value is not None
        assert wrapper_version_value is not None
        assert skill_version_value is not None
        assert artifact_digest_value is not None
        assert profile_digest_value is not None

        if record.revoked_at is not None:
            raise ApiError(
                code="NODE_JOIN_CODE_REVOKED", message="Join code was revoked.", status_code=410
            )
        if node_id is not None and record.node_id != node_id:
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID",
                message="Join code does not match the node.",
                status_code=409,
            )
        node = await self._repository.get_node(record.node_id)
        if node is None or node.status == "disabled":
            raise ApiError(code="NODE_NOT_FOUND", message="Node is unavailable.", status_code=404)
        if record.server_origin != self._settings.public_origin:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="Join code belongs to a different Server origin.",
                status_code=409,
            )
        # 加入记录只是授权快照；交换时必须重验当前发布配置，禁止固定旧运行时。
        current_profile = (
            self._settings.ego_browser_expected_release_profile,
            self._settings.ego_browser_expected_wrapper_version,
            self._settings.ego_browser_expected_skill_version,
            f"sha256:{self._settings.ego_browser_expected_skill_tree_sha256}",
        )
        recorded_profile = (
            release_profile_value,
            wrapper_version_value,
            skill_version_value,
            artifact_digest_value,
        )
        if recorded_profile != current_profile:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="Join code references a stale release profile.",
                status_code=409,
            )
        expected_profile_digest = self._join_profile_digest(
            server_origin=record.server_origin,
            node_id=record.node_id,
            release_profile=release_profile_value,
            wrapper_version=wrapper_version_value,
            skill_version=skill_version_value,
            runtime_version=record.runtime_version,
            artifact_digest=artifact_digest_value,
            ego_browser_enabled=record.ego_browser_enabled,
        )
        if profile_digest_value.lower() != expected_profile_digest:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="The join-code release profile integrity check failed.",
                status_code=409,
            )
        if not require_profile_echo:
            return node

        # 首次登记须回传节点本地制品值；来源和节点摘要仍以 Server 值为准。
        optional_expected_values = {
            "release_profile": (release_profile_value, release_profile),
            "runtime_version": (record.runtime_version, runtime_version),
            "profile_digest": (profile_digest_value, profile_digest),
        }
        for field, (expected, supplied) in optional_expected_values.items():
            if expected is not None and supplied is not None and supplied != expected:
                raise ApiError(
                    code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                    message=f"Join code {field} does not match the approved profile.",
                    status_code=409,
                )
        for field, expected, supplied in (
            ("wrapper_version", wrapper_version_value, wrapper_version),
            ("skill_version", skill_version_value, skill_version),
            ("artifact_digest", artifact_digest_value, artifact_digest),
        ):
            if supplied is None or supplied != expected:
                raise ApiError(
                    code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                    message=f"Join code {field} must match the approved profile.",
                    status_code=409,
                )
        if ego_browser_enabled is not None and ego_browser_enabled != record.ego_browser_enabled:
            raise ApiError(
                code="NODE_JOIN_CODE_PROFILE_MISMATCH",
                message="Join code ego-browser intent does not match the approved profile.",
                status_code=409,
            )
        return node

    def _consumed_join_exchange_result(
        self,
        *,
        record: NodeJoinCode,
        node: Node,
        exchange_id: str,
        now: datetime,
    ) -> NodeJoinCodeExchangeResult:
        """
        返回已消费拼接交换结果。

        :param record (NodeJoinCode): 记录
        :param node (Node): 节点
        :param exchange_id (str): 交换 ID
        :param now (datetime): 当前时间
        :return NodeJoinCodeExchangeResult: 已消费拼接交换结果
        """

        if record.consumed_at is None or record.encrypted_node_token is None:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The enrollment result cannot be recovered.",
                status_code=503,
            )
        if record.exchange_id != exchange_id:
            raise ApiError(
                code="NODE_JOIN_CODE_REPLAYED",
                message="Join code was already consumed.",
                status_code=409,
            )
        if record.exchange_result_expires_at is None:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The enrollment result cannot be recovered.",
                status_code=503,
            )
        if _as_utc(record.exchange_result_expires_at) <= now:
            raise ApiError(
                code="NODE_JOIN_CODE_EXPIRED",
                message="The enrollment recovery window has expired.",
                status_code=410,
            )
        try:
            raw_token = decrypt_text(self._settings.secret_key, record.encrypted_node_token)
        except Exception as exc:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The enrollment result cannot be recovered.",
                status_code=503,
            ) from exc
        recovered_hash = hash_token(self._settings.secret_key, raw_token)
        if node.node_token_hash is None or not secrets.compare_digest(
            recovered_hash, node.node_token_hash
        ):
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The enrollment result no longer matches the Node credential.",
                status_code=503,
            )
        return NodeJoinCodeExchangeResult(
            node=node,
            raw_node_token=raw_token,
            exchange_id=exchange_id,
            server_origin=record.server_origin,
            release_profile=record.release_profile or "",
            wrapper_version=record.wrapper_version or "",
            skill_version=record.skill_version or "",
            runtime_version=record.runtime_version,
            artifact_digest=record.artifact_digest or "",
            profile_digest=record.profile_digest or "",
            ego_browser_enabled_intent=record.ego_browser_enabled,
        )

    async def _recover_join_exchange_after_integrity_error(
        self,
        *,
        attempted_record_id: UUID,
        expected_code_hash: str,
        node_id: UUID | None,
        exchange_id: str,
        release_profile: str | None,
        wrapper_version: str | None,
        skill_version: str | None,
        runtime_version: str | None,
        artifact_digest: str | None,
        profile_digest: str | None,
        ego_browser_enabled: bool | None,
        cause: IntegrityError,
    ) -> NodeJoinCodeExchangeResult:
        """
        返回recover 拼接交换之后完整性错误。

        :param attempted_record_id (UUID): attempted 记录 ID
        :param expected_code_hash (str): 预期代码 hash
        :param node_id (UUID | None): 节点 ID
        :param exchange_id (str): 交换 ID
        :param release_profile (str | None): 发布配置
        :param wrapper_version (str | None): wrapper 版本
        :param skill_version (str | None): skill 版本
        :param runtime_version (str | None): 运行时版本
        :param artifact_digest (str | None): 制品摘要
        :param profile_digest (str | None): 配置摘要
        :param ego_browser_enabled (bool | None): Ego Browser 启用状态
        :param cause (IntegrityError): 原始完整性错误
        :return NodeJoinCodeExchangeResult: recover 拼接交换之后完整性错误
        """

        await self._session.rollback()
        record = await self._session.scalar(
            select(NodeJoinCode).where(NodeJoinCode.exchange_id == exchange_id).with_for_update()
        )
        if record is None:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The enrollment result could not be resolved after a storage conflict.",
                status_code=503,
            ) from cause
        if record.id != attempted_record_id or record.code_hash != expected_code_hash:
            raise ApiError(
                code="NODE_JOIN_CODE_EXCHANGE_CONFLICT",
                message="The exchange ID is already bound to another join code.",
                status_code=409,
            ) from cause
        node = await self._validate_join_exchange_record(
            record=record,
            node_id=node_id,
            expected_code_hash=expected_code_hash,
            release_profile=release_profile,
            wrapper_version=wrapper_version,
            skill_version=skill_version,
            runtime_version=runtime_version,
            artifact_digest=artifact_digest,
            profile_digest=profile_digest,
            ego_browser_enabled=ego_browser_enabled,
            require_profile_echo=True,
        )
        try:
            return self._consumed_join_exchange_result(
                record=record,
                node=node,
                exchange_id=exchange_id,
                now=self._now(),
            )
        except ApiError as exc:
            raise exc from cause

    async def create_node(
        self,
        *,
        actor: User,
        name: str,
        region_code: str,
        tags: list[str],
        weight: int,
        supported_tool_types: list[str],
        allowed_runtime_backends: list[str],
        default_runtime_backend: str,
        runtime_policy: dict[str, object],
        wireguard_ip: str | None = None,
        wireguard_public_key: str | None = None,
        wireguard_endpoint: str | None = None,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        ssh_user: str | None = None,
        ego_browser_enabled: bool = False,
    ) -> NodeRegistrationToken:
        """
        创建节点并签发注册 token

        :param actor (User): 操作人
        :param name (str): 节点名称
        :param region_code (str): 地区代码
        :param tags (list[str]): 节点标签
        :param weight (int): 调度权重
        :param supported_tool_types (list[str]): 支持工具类型
        :param allowed_runtime_backends (list[str]): 管理员允许的运行时
        :param default_runtime_backend (str): 默认运行时
        :param runtime_policy (dict[str, object]): 运行时策略
        :param wireguard_ip (str | None): WireGuard 地址
        :param wireguard_public_key (str | None): WireGuard 公钥
        :param wireguard_endpoint (str | None): WireGuard 端点
        :param ssh_host (str | None): SSH 主机
        :param ssh_port (int | None): SSH 端口
        :param ssh_user (str | None): SSH 用户
        :param ego_browser_enabled (bool): 是否配置 ego-browser 能力
        :return NodeRegistrationToken: 注册 token
        """

        self._validate_runtime_settings(allowed_runtime_backends, default_runtime_backend)
        raw_token = create_opaque_token("nreg")
        node = await self._repository.add_node(
            Node(
                name=name,
                status="offline",
                region_code=region_code,
                tags=tags,
                weight=weight,
                supported_tool_types=supported_tool_types,
                allowed_runtime_backends=allowed_runtime_backends,
                default_runtime_backend=default_runtime_backend,
                runtime_policy=runtime_policy,
                wireguard_ip=wireguard_ip,
                wireguard_public_key=wireguard_public_key,
                wireguard_endpoint=wireguard_endpoint,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                ego_browser_enabled=ego_browser_enabled,
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

    async def issue_join_code(
        self,
        *,
        actor: User,
        node_id: UUID,
        expires_in_seconds: int = 900,
        ego_browser_enabled: bool | None = None,
        exchange_id: str | None = None,
    ) -> NodeJoinCodeIssueResult:
        """
        为指定 Node 签发一次性加入码。

        :param actor (User): 当前管理员
        :param node_id (UUID): 目标 Node 标识
        :param expires_in_seconds (int): 加入码有效秒数
        :param ego_browser_enabled (bool | None): 加入码携带的 ego-browser 意图
        :param exchange_id (str | None): 控制工作站预先持久化的交换标识
        :return NodeJoinCodeIssueResult: 加入码及其发布元数据
        """

        self._require_enrollment()
        if actor.role != "admin":
            raise ApiError(
                code="COMMON_FORBIDDEN", message="Administrator role is required.", status_code=403
            )
        if expires_in_seconds < 60 or expires_in_seconds > 1800:
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID",
                message="Join-code expiry is invalid.",
                status_code=422,
            )
        if exchange_id is not None and not self._valid_join_exchange_id(exchange_id):
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID",
                message="Join-code exchange ID is invalid.",
                status_code=422,
            )
        if exchange_id is not None and await self._session.scalar(
            select(NodeJoinCode.id).where(NodeJoinCode.exchange_id == exchange_id)
        ):
            raise ApiError(
                code="NODE_JOIN_CODE_EXCHANGE_CONFLICT",
                message="The exchange ID is already bound to another join code.",
                status_code=409,
            )
        node = await self._require_node(node_id)
        profile = self._join_profile(node, ego_browser_enabled=ego_browser_enabled)
        raw_code = "jcode_" + secrets.token_urlsafe(24)
        expires_at = self._now() + timedelta(seconds=expires_in_seconds)
        record = NodeJoinCode(
            node_id=node.id,
            issuer_user_id=actor.id,
            code_hash=hash_token(self._settings.secret_key, raw_code),
            server_origin=profile.server_origin,
            release_profile=profile.release_profile,
            wrapper_version=profile.wrapper_version,
            skill_version=profile.skill_version,
            runtime_version=profile.runtime_version,
            artifact_digest=profile.artifact_digest,
            profile_digest=profile.profile_digest,
            ego_browser_enabled=ego_browser_enabled,
            exchange_id=exchange_id,
            expires_at=expires_at,
        )
        self._session.add(record)
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.join_code.issued",
            target_type="node",
            target_id=str(node.id),
            details={
                "expires_at": expires_at.isoformat(),
                **({"exchange_id": exchange_id} if exchange_id is not None else {}),
            },
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if exchange_id is None:
                raise
            raise ApiError(
                code="NODE_JOIN_CODE_EXCHANGE_CONFLICT",
                message="The exchange ID is already bound to another join code.",
                status_code=409,
            ) from exc
        return NodeJoinCodeIssueResult(
            node=node,
            raw_code=raw_code,
            expires_at=expires_at,
            ego_browser_enabled=ego_browser_enabled,
            server_origin=profile.server_origin,
            release_profile=profile.release_profile,
            wrapper_version=profile.wrapper_version,
            skill_version=profile.skill_version,
            runtime_version=profile.runtime_version,
            artifact_digest=profile.artifact_digest,
            profile_digest=profile.profile_digest,
        )

    async def revoke_join_codes(
        self, *, actor: User, node_id: UUID, exchange_id: str | None = None
    ) -> Literal["revoked", "consumed", "missing"]:
        """
        撤销指定 Node 尚未消费的加入码。

        :param actor (User): 当前管理员
        :param node_id (UUID): 目标 Node 标识
        :param exchange_id (str | None): 只撤销该交换；省略时撤销全部未消费记录
        :return Literal["revoked", "consumed", "missing"]: 加入码的最终状态
        """

        if actor.role != "admin":
            raise ApiError(
                code="COMMON_FORBIDDEN", message="Administrator role is required.", status_code=403
            )
        await self._require_node(node_id)
        if exchange_id is not None and not self._valid_join_exchange_id(exchange_id):
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID",
                message="Join-code exchange ID is invalid.",
                status_code=422,
            )
        if exchange_id is not None:
            record = await self._session.scalar(
                select(NodeJoinCode)
                .where(
                    NodeJoinCode.node_id == node_id,
                    NodeJoinCode.exchange_id == exchange_id,
                )
                .with_for_update()
            )
            if record is None:
                state: Literal["revoked", "consumed", "missing"] = "missing"
                result: list[NodeJoinCode] = []
            elif record.consumed_at is not None:
                state = "consumed"
                result = []
            else:
                state = "revoked"
                result = [] if record.revoked_at is not None else [record]
        else:
            state = "revoked"
            result = list(
                await self._session.scalars(
                    select(NodeJoinCode).where(
                        NodeJoinCode.node_id == node_id,
                        NodeJoinCode.consumed_at.is_(None),
                        NodeJoinCode.revoked_at.is_(None),
                    )
                )
            )
        now = self._now()
        for record in result:
            record.revoked_at = now
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.join_code.revoked",
            target_type="node",
            target_id=str(node_id),
            details={**({"exchange_id": exchange_id} if exchange_id is not None else {})},
        )
        await self._session.commit()
        return state

    async def exchange_join_code(
        self,
        *,
        node_id: UUID | None,
        version: str,
        join_code: str | None,
        exchange_id: str,
        release_profile: str | None = None,
        wrapper_version: str | None = None,
        skill_version: str | None = None,
        runtime_version: str | None = None,
        artifact_digest: str | None = None,
        profile_digest: str | None = None,
        ego_browser_enabled: bool | None = None,
    ) -> NodeJoinCodeExchangeResult:
        """
        原子消费加入码并返回可恢复的 Node token。

        :param node_id (UUID | None): 请求中的 Node 标识
        :param version (str): 加入协议版本
        :param join_code (str | None): 一次性加入码
        :param exchange_id (str): 幂等交换标识
        :param release_profile (str | None): 本地发布配置档案
        :param wrapper_version (str | None): wrapper 版本
        :param skill_version (str | None): Skill 版本
        :param runtime_version (str | None): 运行时版本
        :param artifact_digest (str | None): 制品摘要
        :param profile_digest (str | None): 发布配置摘要
        :param ego_browser_enabled (bool | None): 请求的 ego-browser 能力意图
        :return NodeJoinCodeExchangeResult: Node 凭据及验证后的发布元数据
        """

        self._require_enrollment()
        if join_code is not None and (
            len(join_code) < 16
            or len(join_code) > 4096
            or any(ord(character) < 0x21 or ord(character) == 0x7F for character in join_code)
        ):
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID",
                message="Join code or exchange ID is invalid.",
                status_code=422,
            )
        if not self._valid_join_exchange_id(exchange_id):
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID",
                message="Join code or exchange ID is invalid.",
                status_code=422,
            )
        # 消费前先解析已关联交换，让重复交换 ID 显式失败而非依赖提交冲突。
        exchange_record = await self._session.scalar(
            select(NodeJoinCode).where(NodeJoinCode.exchange_id == exchange_id)
        )
        code_hash = (
            hash_token(self._settings.secret_key, join_code) if join_code is not None else None
        )
        if join_code is None:
            # 提交后响应可能丢失；仅已消费记录可凭持久化交换 ID 恢复响应。
            record = await self._session.scalar(
                select(NodeJoinCode)
                .where(
                    NodeJoinCode.exchange_id == exchange_id,
                    NodeJoinCode.consumed_at.is_not(None),
                )
                .with_for_update()
            )
        else:
            assert code_hash is not None
            record = await self._session.scalar(
                select(NodeJoinCode).where(NodeJoinCode.code_hash == code_hash).with_for_update()
            )
            if exchange_record is not None and (record is None or exchange_record.id != record.id):
                raise ApiError(
                    code="NODE_JOIN_CODE_EXCHANGE_CONFLICT",
                    message="The exchange ID is already bound to another join code.",
                    status_code=409,
                )
        if record is None:
            raise ApiError(
                code="NODE_JOIN_CODE_INVALID", message="Join code is invalid.", status_code=401
            )
        now = self._now()
        node = await self._validate_join_exchange_record(
            record=record,
            node_id=node_id,
            expected_code_hash=code_hash,
            release_profile=release_profile,
            wrapper_version=wrapper_version,
            skill_version=skill_version,
            runtime_version=runtime_version,
            artifact_digest=artifact_digest,
            profile_digest=profile_digest,
            ego_browser_enabled=ego_browser_enabled,
            require_profile_echo=join_code is not None,
        )
        if record.consumed_at is not None:
            return self._consumed_join_exchange_result(
                record=record,
                node=node,
                exchange_id=exchange_id,
                now=now,
            )

        # 已消费短期码即使过期仍可恢复原响应；未消费短期码按常规过期。
        if _as_utc(record.expires_at) <= now:
            raise ApiError(
                code="NODE_JOIN_CODE_EXPIRED", message="Join code has expired.", status_code=410
            )

        attempted_record_id = record.id
        assert code_hash is not None
        try:
            # 先预留全局唯一交换 ID，确保竞态失败方不会生成第二份凭据。
            record.exchange_id = exchange_id
            await self._session.flush([record])

            # 加入码意图仅支配首次登记或显式回传；普通重装必须保留管理员现有选择。
            first_enrollment = node.node_token_hash is None
            raw_token = create_opaque_token("node")
            node.node_token_hash = hash_token(self._settings.secret_key, raw_token)
            node.registration_token_hash = None
            node.version = version
            node.status = "healthy"
            node.last_heartbeat_at = now
            if record.ego_browser_enabled is not None and (
                first_enrollment or ego_browser_enabled is not None
            ):
                node.ego_browser_enabled = record.ego_browser_enabled
            record.consumed_at = now
            record.encrypted_node_token = encrypt_text(self._settings.secret_key, raw_token)
            record.exchange_result_expires_at = now + timedelta(hours=24)
            await self._audit(
                actor_user_id=None,
                action="node_api.join_code.exchange",
                target_type="node",
                target_id=str(node.id),
                details={"version": version, "exchange_id": exchange_id},
            )
            await self._session.commit()
        except IntegrityError as exc:
            return await self._recover_join_exchange_after_integrity_error(
                attempted_record_id=attempted_record_id,
                expected_code_hash=code_hash,
                node_id=node_id,
                exchange_id=exchange_id,
                release_profile=release_profile,
                wrapper_version=wrapper_version,
                skill_version=skill_version,
                runtime_version=runtime_version,
                artifact_digest=artifact_digest,
                profile_digest=profile_digest,
                ego_browser_enabled=ego_browser_enabled,
                cause=exc,
            )
        return NodeJoinCodeExchangeResult(
            node=node,
            raw_node_token=raw_token,
            exchange_id=exchange_id,
            server_origin=record.server_origin,
            release_profile=record.release_profile or "",
            wrapper_version=record.wrapper_version or "",
            skill_version=record.skill_version or "",
            runtime_version=record.runtime_version,
            artifact_digest=record.artifact_digest or "",
            profile_digest=record.profile_digest or "",
            ego_browser_enabled_intent=record.ego_browser_enabled,
        )

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
        :raises ApiError: 注册 token 无效或节点已被禁用
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
        :raises ApiError: Node token 无效或节点已被禁用
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
        wireguard_ip: str | None,
        wireguard_public_key: str | None,
        wireguard_endpoint: str | None,
        resources: dict[str, object],
        runtime: dict[str, object],
    ) -> None:
        """
        提交节点心跳

        :param node (Node): 当前节点
        :param node_id (UUID): 请求中的节点 ID
        :param version (str): 节点版本
        :param supported_tool_types (list[str]): 支持工具类型
        :param wireguard_ip (str | None): WireGuard 地址
        :param wireguard_public_key (str | None): WireGuard 公钥
        :param wireguard_endpoint (str | None): WireGuard 连接端点
        :param resources (dict[str, object]): 资源快照
        :param runtime (dict[str, object]): 运行时快照
        :raises ApiError: Node 凭据与心跳中的节点 ID 不一致
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
        if wireguard_ip:
            node.wireguard_ip = wireguard_ip
        if wireguard_public_key:
            node.wireguard_public_key = wireguard_public_key
        if wireguard_endpoint:
            node.wireguard_endpoint = wireguard_endpoint
        node.last_heartbeat_at = now
        capabilities = runtime.get("runtime_capabilities")
        node.runtime_capabilities = self._normalize_runtime_capabilities(
            capabilities,
            configured_enabled=node.ego_browser_enabled,
        )
        node.status = "healthy" if self._runtime_is_healthy(node, runtime) else "degraded"
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

    def _normalize_runtime_capabilities(
        self,
        value: object,
        *,
        configured_enabled: bool | None = None,
    ) -> dict[str, object]:
        """
        规范化节点能力并把服务端执行闸门投影到 ego-browser 能力。

        :param value (object): 值
        :param configured_enabled (bool | None): configured 启用状态
        :return dict[str, object]: 运行时能力
        """

        if not isinstance(value, dict):
            return {}
        normalized = dict(value)
        raw_bridge = normalized.get("ego_browser_bridge")
        if isinstance(raw_bridge, dict):
            bridge = dict(raw_bridge)
            admission_fields = {
                "configured_enabled",
                "effective_enabled",
                "node_execution_allowed",
            }
            present = admission_fields.intersection(bridge)
            # 不为旧心跳伪造准入字段；新版心跳的部分或畸形投影必须清除可执行元数据。
            if present and present != admission_fields:
                bridge["effective_enabled"] = False
                bridge["node_execution_allowed"] = False
                bridge["supported"] = False
                bridge["protocol_versions"] = []
                bridge["backends"] = []
            elif present == admission_fields:
                configured = (
                    configured_enabled
                    if isinstance(configured_enabled, bool)
                    else bridge.get("configured_enabled")
                )
                effective = bridge.get("effective_enabled")
                execution_allowed = bridge.get("node_execution_allowed")
                valid_booleans = all(
                    isinstance(item, bool) for item in (configured, effective, execution_allowed)
                )
                if not valid_booleans:
                    configured = False
                    effective = False
                    execution_allowed = False
                else:
                    # 持久化意图优先，心跳不能以 true 提升已禁用节点。
                    configured = bool(configured)
                    effective = bool(effective) and configured
                    execution_allowed = bool(execution_allowed)
                effective = bool(effective)
                # 节点不能自行授予执行权；登记门禁只管理身份操作。
                execution_allowed = (
                    effective and execution_allowed and self._settings.ego_browser_bridge_enabled
                )
                bridge["configured_enabled"] = configured
                bridge["effective_enabled"] = effective
                bridge["node_execution_allowed"] = execution_allowed
                if not execution_allowed:
                    bridge["supported"] = False
                    bridge["protocol_versions"] = []
                    bridge["backends"] = []
            elif not self._settings.ego_browser_bridge_enabled:
                # 旧心跳可能早于门禁切换，进入认领或中继路径前必须清理快照。
                bridge["supported"] = False
                bridge["protocol_versions"] = []
                bridge["backends"] = []
            normalized["ego_browser_bridge"] = bridge
        return normalized

    async def list_nodes(self) -> list[Node]:
        """
        列出节点并标记过期离线

        :return list[Node]: 节点列表
        """

        nodes = list(await self._repository.list_nodes())
        await self._mark_stale_nodes(nodes)
        return nodes

    async def get_node(self, node_id: UUID) -> Node:
        """
        读取节点并标记过期离线

        :param node_id (UUID): 节点 ID
        :return Node: 节点实体
        """

        node = await self._require_node(node_id)
        await self._mark_stale_nodes([node])
        return node

    async def expire_stale_nodes(self) -> int:
        """
        主动标记心跳超时节点并撤销其 browser binding。

        :return int: 本次标记为离线的节点数量
        """

        nodes = list(await self._repository.list_nodes())
        return await self._mark_stale_nodes(nodes)

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
        allowed_runtime_backends: list[str] | None,
        default_runtime_backend: str | None,
        runtime_policy: dict[str, object] | None,
        wireguard_ip: str | None,
        wireguard_public_key: str | None,
        wireguard_endpoint: str | None,
        ssh_host: str | None,
        ssh_port: int | None,
        ssh_user: str | None,
        ego_browser_enabled: bool | None = None,
    ) -> Node:
        """
        更新节点

        :param actor (User): 操作人
        :param node_id (UUID): 节点 ID
        :param name (str | None): 节点名称
        :param status (str | None): 节点状态
        :param tags (list[str] | None): 节点标签
        :param weight (int | None): 权重
        :param supported_tool_types (list[str] | None): 支持工具类型
        :param allowed_runtime_backends (list[str] | None): 管理员允许的运行时
        :param default_runtime_backend (str | None): 默认运行时
        :param runtime_policy (dict[str, object] | None): 运行时策略
        :param wireguard_ip (str | None): WireGuard 地址
        :param wireguard_public_key (str | None): WireGuard 公钥
        :param wireguard_endpoint (str | None): WireGuard 端点
        :param ssh_host (str | None): SSH 主机
        :param ssh_port (int | None): SSH 端口
        :param ssh_user (str | None): SSH 用户
        :param ego_browser_enabled (bool | None): 是否变更 ego-browser 能力配置
        :return Node: 节点实体
        """

        node = await self._require_node(node_id)
        effective_allowed = (
            allowed_runtime_backends
            if allowed_runtime_backends is not None
            else node.allowed_runtime_backends
        )
        effective_default = (
            default_runtime_backend
            if default_runtime_backend is not None
            else node.default_runtime_backend
        )
        self._validate_runtime_settings(effective_allowed, effective_default)
        ego_browser_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        if name is not None:
            node.name = name
        if status is not None:
            node.status = status
            if status not in {"healthy", "degraded", "active"}:
                await ego_browser_service.revoke_for_node(
                    node_id=node.id,
                    reason="node_unavailable",
                    commit=False,
                    publish=False,
                )
                await revoke_port_forwards(
                    self._session,
                    reason="node_revoked",
                    actor_user_id=actor.id,
                    node_id=node.id,
                )
        if tags is not None:
            node.tags = tags
        if weight is not None:
            node.weight = weight
        if supported_tool_types is not None:
            node.supported_tool_types = supported_tool_types
        if allowed_runtime_backends is not None:
            node.allowed_runtime_backends = allowed_runtime_backends
        if default_runtime_backend is not None:
            node.default_runtime_backend = default_runtime_backend
        if runtime_policy is not None:
            node.runtime_policy = runtime_policy
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
        if ego_browser_enabled is not None:
            node.ego_browser_enabled = ego_browser_enabled
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.update",
            target_type="node",
            target_id=str(node.id),
            details={"status": status} if status else {},
        )
        await self._session.commit()
        await ego_browser_service.publish_pending_revocations()
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
            allowed_runtime_backends=None,
            default_runtime_backend=None,
            runtime_policy=None,
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
            allowed_runtime_backends=None,
            default_runtime_backend=None,
            runtime_policy=None,
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

    async def delete_node(self, *, actor: User, node_id: UUID) -> None:
        """
        删除已禁用且无业务引用的节点

        :param actor (User): 操作人
        :param node_id (UUID): 节点 ID
        :raises ApiError: 节点未禁用或仍有业务引用与浏览器 binding 历史
        """

        node = await self._require_node(node_id)
        if node.status != "disabled":
            raise ApiError(
                code="NODE_DELETE_REQUIRES_DISABLED",
                message="Disable the node before deleting it.",
                status_code=409,
            )
        if await self._repository.has_business_references(node.id):
            raise ApiError(
                code="NODE_DELETE_BLOCKED",
                message="The node is still referenced by accounts or session history.",
                status_code=409,
            )
        if await EgoBrowserRepository(self._session).has_any_for_node(node.id):
            raise ApiError(
                code="NODE_DELETE_BROWSER_BINDING_HISTORY",
                message="Ego-browser binding history must expire before deleting the node.",
                status_code=409,
            )
        await self._audit(
            actor_user_id=actor.id,
            action="nodes.delete",
            target_type="node",
            target_id=str(node.id),
            details={"name": node.name},
        )
        await self._repository.delete_node(node)
        await self._session.commit()

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
        :param payload (dict[str, object]): 任务 payload
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

    def _validate_runtime_settings(
        self, allowed_runtime_backends: list[str], default_runtime_backend: str
    ) -> None:
        """
        校验管理员运行时策略

        :param allowed_runtime_backends (list[str]): 允许的运行时
        :param default_runtime_backend (str): 默认运行时
        """

        allowed = set(allowed_runtime_backends)
        if not allowed or not allowed.issubset(RUNTIME_BACKENDS):
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Runtime backends are invalid.",
                status_code=422,
            )
        if default_runtime_backend not in allowed:
            raise ApiError(
                code="COMMON_VALIDATION_ERROR",
                message="Default runtime backend must be allowed.",
                status_code=422,
            )

    def _runtime_is_healthy(self, node: Node, runtime: dict[str, object]) -> bool:
        """
        判断节点是否至少有一个可调度运行时

        :param node (Node): 节点实体
        :param runtime (dict[str, object]): 心跳运行时快照
        :return bool: 是否健康
        """

        if not runtime.get("tmux_ok"):
            return False
        capabilities = runtime.get("runtime_capabilities")
        if not isinstance(capabilities, dict) or not capabilities:
            return bool(runtime.get("docker_ok"))
        backends = capabilities.get("backends")
        if not isinstance(backends, list):
            return False
        available = {item for item in backends if isinstance(item, str)}
        return bool(available.intersection(node.allowed_runtime_backends))

    async def poll_tasks(self, *, node: Node, limit: int = 1) -> list[NodeTask]:
        """
        租约节点任务

        :param node (Node): 当前节点
        :param limit (int): 最大任务数
        :return list[NodeTask]: 节点任务列表
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
        :raises ApiError: 节点任务已经进入终态
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
        :param result (dict[str, object]): 任务结果
        """

        task = await self._require_node_task(node=node, task_id=task_id)
        result = _content_safe_task_completion(task, result)
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
        await self._apply_sync_session_task_result(task, result)
        await self._apply_browser_session_task_result(task, result)
        ego_browser_service = await self._revoke_ego_browser_for_task(task, result)
        await self._session.commit()
        await ego_browser_service.publish_pending_revocations()

    async def fail_task(self, *, node: Node, task_id: str, error: dict[str, object]) -> None:
        """
        标记任务失败

        :param node (Node): 当前节点
        :param task_id (str): 任务 ID
        :param error (dict[str, object]): 错误信息
        """

        task = await self._require_node_task(node=node, task_id=task_id)
        error = _content_safe_task_failure(task, error)
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
        await self._apply_sync_session_task_failure(task)
        await self._apply_browser_session_task_failure(task, error)
        ego_browser_service = await self._revoke_ego_browser_for_task(task, error)
        await self._session.commit()
        await ego_browser_service.publish_pending_revocations()

    async def reconcile(
        self, *, node: Node, node_id: UUID, sections: list[str], snapshot: dict[str, object]
    ) -> None:
        """
        接收节点对账快照

        :param node (Node): 当前节点
        :param node_id (UUID): 请求节点 ID
        :param sections (list[str]): 对账分区
        :param snapshot (dict[str, object]): 对账快照
        :raises ApiError: Node 凭据与快照中的节点 ID 不一致
        """

        if node.id != node_id:
            raise ApiError(
                code="COMMON_FORBIDDEN",
                message="Node credential does not match node.",
                status_code=403,
            )
        interrupted_count = 0
        cleanup_count = 0
        revoked_bindings: list[RevokedDeviceBinding] = []
        if "runtime_sessions" in sections:
            reported = self._reported_runtime_sessions(snapshot)
            active_sessions = await self._repository.list_active_sessions_for_node(node.id)
            for tool_session in active_sessions:
                if tool_session.runtime_backend not in {"native", "docker_sandbox"}:
                    continue
                runtime = reported.get(str(tool_session.id))
                if runtime is None and tool_session.runtime_backend == "docker_sandbox":
                    continue
                if runtime is not None and runtime.get("active") is True:
                    continue
                device_stop = await DeviceSessionService(
                    self._session, self._settings, self._relay_hub
                ).stop_for_tool_session(
                    tool_session_id=tool_session.id,
                    reason="node_reconcile",
                    audit_action="device_session.node_reconcile",
                    commit=False,
                )
                revoked_bindings.extend(device_stop.revoked_bindings)
                ego_service = EgoBrowserService(
                    self._session,
                    self._settings,
                    revocation_publisher=self._ego_browser_revocation_publisher,
                )
                await ego_service.revoke_for_tool_session(
                    tool_session_id=tool_session.id,
                    reason="node_reconcile",
                    commit=False,
                    publish=False,
                )
                if runtime is not None and runtime.get("exit_reason") == "process_exited":
                    await self._enqueue_reconciled_session_cleanup(tool_session)
                    cleanup_count += 1
                else:
                    tool_session.status = "interrupted"
                interrupted_count += 1
        await self._audit(
            actor_user_id=None,
            action="node_api.reconcile",
            target_type="node",
            target_id=str(node.id),
            details={
                "sections": sections,
                "snapshot_keys": sorted(snapshot),
                "interrupted_count": interrupted_count,
                "cleanup_count": cleanup_count,
            },
        )
        await self._session.commit()
        await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).close_revoked_bindings(revoked_bindings)
        await EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        ).publish_pending_revocations()

    async def _revoke_ego_browser_for_task(
        self, task: NodeTask, fallback: dict[str, object]
    ) -> EgoBrowserService:
        """
        在节点任务使工具 session 终止后撤销其 ego-browser binding。

        :param task (NodeTask): 任务
        :param fallback (dict[str, object]): 回退数据
        :return EgoBrowserService: Ego Browser 对应任务
        """

        service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        await service.reconcile_cancel_task(
            task=task,
            result=fallback,
            succeeded=task.status == "succeeded",
        )
        session_id = self._task_session_id(task, fallback)
        if session_id is None:
            return service
        tool_session = await self._session.get(Session, session_id)
        if tool_session is not None and tool_session.status in {
            "stopped",
            "interrupted",
            "failed",
        }:
            await service.revoke_for_tool_session(
                tool_session_id=session_id,
                reason="tool_session_terminal",
                commit=False,
                publish=False,
            )
        return service

    async def _enqueue_reconciled_session_cleanup(self, tool_session: Session) -> None:
        """
        将其加入队列已协调会话清理。

        :param tool_session (Session): 工具会话
        """
        task_id = f"cleanup_tool_session:{tool_session.id}"
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
                        "preserve_interrupted_status": True,
                    },
                    retry_count=0,
                )
            )
        tool_session.status = "interrupted"
        await self._audit(
            actor_user_id=None,
            action="sessions.auto_cleanup",
            target_type="session",
            target_id=str(tool_session.id),
            details={"task_id": task_id, "reason": "process_exited"},
        )

    def _reported_runtime_sessions(
        self, snapshot: dict[str, object]
    ) -> dict[str, dict[str, object]]:
        """
        解析节点上报的非敏感运行时会话摘要

        :param snapshot (dict[str, object]): 节点对账快照
        :return dict[str, dict[str, object]]: 以会话 ID 索引的有效摘要
        """

        items = snapshot.get("sessions")
        if not isinstance(items, list):
            return {}
        reported: dict[str, dict[str, object]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            session_id = item.get("session_id")
            backend = item.get("runtime_backend")
            if isinstance(session_id, str) and backend in {"native", "docker_sandbox"}:
                reported[session_id] = item
        return reported

    async def _require_node(self, node_id: UUID) -> Node:
        """
        获取并校验节点。

        :param node_id (UUID): 节点 ID
        :return Node: 节点
        """
        node = await self._repository.get_node(node_id)
        if node is None:
            raise ApiError(code="COMMON_NOT_FOUND", message="Node was not found.", status_code=404)
        return node

    async def _require_node_task(self, *, node: Node, task_id: str) -> NodeTask:
        """
        获取并校验节点任务。

        :param node (Node): 节点
        :param task_id (str): 任务 ID
        :return NodeTask: 节点任务
        """
        task = await self._repository.get_task_by_task_id(task_id)
        if task is None or task.node_id != node.id:
            raise ApiError(code="COMMON_NOT_FOUND", message="Task was not found.", status_code=404)
        return task

    async def _apply_tool_account_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        """
        应用工具账号任务结果。

        :param task (NodeTask): 任务
        :param result (dict[str, object]): 结果
        """
        account_id = self._task_tool_account_id(task, result)
        if account_id is None:
            return
        account = await self._session.get(ToolAccount, account_id)
        if account is None:
            return
        profile = await self._tool_account_profile(account)
        if task.task_type == "migrate_tool_account_runtime":
            source = task.payload.get("source_runtime_backend")
            target = task.payload.get("target_runtime_backend")
            migration = profile.profile_json.get("runtime_migration")
            previous_status = (
                migration.get("previous_status") if isinstance(migration, dict) else None
            )
            if (
                result.get("migrated") is True
                and isinstance(target, str)
                and self._text_result(result, "runtime_backend") == target
            ):
                account.runtime_backend = target
                account.status = previous_status if isinstance(previous_status, str) else "active"
                profile.profile_json = {
                    **profile.profile_json,
                    "runtime_migration": {
                        **(migration if isinstance(migration, dict) else {}),
                        "status": "succeeded",
                        "backup_path": self._text_result(result, "backup_path"),
                    },
                }
                return
            if isinstance(source, str):
                account.runtime_backend = source
            account.status = previous_status if isinstance(previous_status, str) else "failed"
            return
        if task.task_type == "create_binding_session":
            if profile.profile_json.get("binding_task_id") != task.task_id:
                return
            runtime_backend = self._text_result(result, "runtime_backend")
            if runtime_backend is not None:
                account.runtime_backend = runtime_backend
            status = result.get("status")
            if status in {"waiting_user_login", "ready"}:
                account.status = "binding_waiting_user_login"
                profile.profile_json = {
                    **profile.profile_json,
                    "binding_session_id": self._text_result(result, "binding_session_id"),
                    "tmux_session_name": self._text_result(result, "tmux_session_name"),
                    "account_remote_path": self._text_result(result, "account_remote_path"),
                    "last_binding_result": result,
                    "last_error": None,
                }
                return
            account.status = "failed"
            profile.profile_json = {
                **profile.profile_json,
                "last_error": self._text_result(result, "error") or "Binding session failed.",
            }
            return
        if task.task_type == "verify_tool_account":
            if profile.profile_json.get("verification_task_id") != task.task_id:
                return
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
        """
        应用工具账号任务失败。

        :param task (NodeTask): 任务
        :param error (dict[str, object]): 错误
        """
        if task.task_type not in {
            "create_binding_session",
            "verify_tool_account",
            "migrate_tool_account_runtime",
        }:
            return
        account_id = self._task_tool_account_id(task, error)
        if account_id is None:
            return
        account = await self._session.get(ToolAccount, account_id)
        if account is None:
            return
        profile = await self._tool_account_profile(account)
        if (
            task.task_type == "create_binding_session"
            and profile.profile_json.get("binding_task_id") != task.task_id
        ):
            return
        if (
            task.task_type == "verify_tool_account"
            and profile.profile_json.get("verification_task_id") != task.task_id
        ):
            return
        if task.task_type == "migrate_tool_account_runtime":
            migration = profile.profile_json.get("runtime_migration")
            previous_status = (
                migration.get("previous_status") if isinstance(migration, dict) else None
            )
            source = task.payload.get("source_runtime_backend")
            if isinstance(source, str):
                account.runtime_backend = source
            account.status = previous_status if isinstance(previous_status, str) else "failed"
            profile.profile_json = {
                **profile.profile_json,
                "runtime_migration": {
                    **(migration if isinstance(migration, dict) else {}),
                    "status": "failed",
                    "error": self._text_result(error, "message")
                    or self._text_result(error, "error"),
                },
            }
            return
        account.status = "failed"
        profile.profile_json = {
            **profile.profile_json,
            "last_error": self._text_result(error, "message") or self._text_result(error, "error"),
        }

    async def _apply_tool_session_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        """
        应用工具会话任务结果。

        :param task (NodeTask): 任务
        :param result (dict[str, object]): 结果
        """
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
                runtime_resource_id = self._text_result(result, "runtime_resource_id")
                if runtime_resource_id is None:
                    runtime_resource_id = container_id
                if runtime_resource_id is not None:
                    tool_session.runtime_resource_id = runtime_resource_id
                return
            tool_session.status = "failed"
            return
        if task.task_type == "stop_tool_session":
            if task.payload.get("preserve_interrupted_status") is True:
                tool_session.status = "interrupted"
                return
            tool_session.status = "stopped"

    async def _apply_sync_session_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        """
        应用 workspace 准备任务结果

        :param task (NodeTask): 节点任务
        :param result (dict[str, object]): 任务结果
        """

        if task.task_type != "prepare_workspace" or result.get("status") != "prepared":
            return
        value = task.payload.get("sync_session_id")
        if not isinstance(value, str):
            return
        try:
            sync_session = await self._session.get(SyncSession, UUID(value))
        except ValueError:
            return
        if sync_session is not None:
            sync_session.status = "active"

    async def _apply_sync_session_task_failure(self, task: NodeTask) -> None:
        """
        将 workspace 准备失败同步到同步会话

        :param task (NodeTask): 节点任务
        """

        if task.task_type != "prepare_workspace":
            return
        value = task.payload.get("sync_session_id")
        if not isinstance(value, str):
            return
        try:
            sync_session = await self._session.get(SyncSession, UUID(value))
        except ValueError:
            return
        if sync_session is not None:
            sync_session.status = "failed"

    async def _apply_tool_session_task_failure(
        self, task: NodeTask, error: dict[str, object]
    ) -> None:
        """
        应用工具会话任务失败。

        :param task (NodeTask): 任务
        :param error (dict[str, object]): 错误
        """
        session_id = self._task_session_id(task, error)
        if session_id is None:
            return
        tool_session = await self._session.get(Session, session_id)
        if tool_session is None:
            return
        if task.task_type == "stop_tool_session":
            if task.payload.get("preserve_interrupted_status") is True:
                tool_session.status = "interrupted"
                return
            tool_session.status = "failed"
            return
        if task.task_type == "create_tool_session":
            tool_session.status = "failed"

    async def _apply_browser_session_task_result(
        self, task: NodeTask, result: dict[str, object]
    ) -> None:
        """
        应用浏览器会话任务结果。

        :param task (NodeTask): 任务
        :param result (dict[str, object]): 结果
        """
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
        """
        应用浏览器会话任务失败。

        :param task (NodeTask): 任务
        :param error (dict[str, object]): 错误
        """
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
        """
        返回工具账号配置。

        :param account (ToolAccount): 账号
        :return ToolAccountProfile: 工具账号配置
        """
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
        """
        返回任务工具账号 ID。

        :param task (NodeTask): 任务
        :param fallback (dict[str, object]): 回退数据
        :return UUID | None: 任务工具账号 ID
        """
        value = task.payload.get("tool_account_id") or fallback.get("tool_account_id")
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    def _task_session_id(self, task: NodeTask, fallback: dict[str, object]) -> UUID | None:
        """
        返回任务会话 ID。

        :param task (NodeTask): 任务
        :param fallback (dict[str, object]): 回退数据
        :return UUID | None: 任务会话 ID
        """
        value = task.payload.get("session_id") or fallback.get("session_id")
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    def _task_browser_session_id(self, task: NodeTask, fallback: dict[str, object]) -> UUID | None:
        """
        返回任务浏览器会话 ID。

        :param task (NodeTask): 任务
        :param fallback (dict[str, object]): 回退数据
        :return UUID | None: 任务浏览器会话 ID
        """
        value = task.payload.get("browser_session_id") or fallback.get("browser_session_id")
        if not isinstance(value, str):
            return None
        try:
            return UUID(value)
        except ValueError:
            return None

    def _text_result(self, result: dict[str, object], key: str) -> str | None:
        """
        返回文本结果。

        :param result (dict[str, object]): 结果
        :param key (str): 键
        :return str | None: 文本结果
        """
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

    async def _mark_stale_nodes(self, nodes: list[Node]) -> int:
        """
        标记过期节点。

        :param nodes (list[Node]): 节点
        :return int: 过期节点
        """
        stale_nodes: list[Node] = []
        cutoff = self._now() - timedelta(seconds=self._settings.node_offline_after_seconds)
        for node in nodes:
            if node.status not in {"healthy", "degraded"} or node.last_heartbeat_at is None:
                continue
            heartbeat_at = (
                node.last_heartbeat_at
                if node.last_heartbeat_at.tzinfo
                else node.last_heartbeat_at.replace(tzinfo=UTC)
            )
            if heartbeat_at < cutoff:
                node.status = "offline"
                stale_nodes.append(node)
        if not stale_nodes:
            return 0

        ego_browser_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        for node in stale_nodes:
            await ego_browser_service.revoke_for_node(
                node_id=node.id,
                reason="node_heartbeat_lost",
                commit=False,
                publish=False,
            )
        await self._session.commit()
        await ego_browser_service.publish_pending_revocations()
        return len(stale_nodes)

    def _now(self) -> datetime:
        """
        获取当前时间。

        :return datetime: 当前时间
        """
        return datetime.now(UTC)


def _content_safe_task_completion(task: NodeTask, result: dict[str, object]) -> dict[str, object]:
    """
    返回内容安全任务完成结果。

    :param task (NodeTask): 任务
    :param result (dict[str, object]): 结果
    :return dict[str, object]: 内容安全任务完成结果
    """
    if task.task_type != "cancel_ego_browser_request":
        return result
    if (
        set(result) == _EGO_BROWSER_CANCEL_RESULT_KEYS
        and result.get("status") == "cancellation_completed"
        and isinstance(result.get("request_active"), bool)
        and isinstance(result.get("server_terminal_observed"), bool)
    ):
        return {
            "status": "cancellation_completed",
            "request_active": result["request_active"],
            "server_terminal_observed": result["server_terminal_observed"],
        }
    return {"status": "cancellation_unconfirmed"}


def _content_safe_task_failure(task: NodeTask, error: dict[str, object]) -> dict[str, object]:
    """
    返回内容安全任务失败。

    :param task (NodeTask): 任务
    :param error (dict[str, object]): 错误
    :return dict[str, object]: 内容安全任务失败
    """
    if task.task_type != "cancel_ego_browser_request":
        return error
    return {
        "code": "EGO_BROWSER_CANCELLATION_FAILED",
        "message": "Cancellation could not be confirmed.",
    }
