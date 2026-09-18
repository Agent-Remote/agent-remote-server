"""
封装 ego-browser 服务共享的状态、校验与事务操作。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal, NoReturn
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.ego_browser.relay import (
    EGO_BROWSER_PROTOCOL,
    EgoBrowserProofChallengeClaims,
    EgoBrowserRelayStore,
    EgoBrowserRevocationPublisher,
)
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    EgoBrowserBinding,
    EgoBrowserDevice,
    EgoBrowserRevocationOutbox,
    Node,
    User,
)
from agent_remote_server.models.ego_browser import MAX_ACTIVE_EGO_BROWSER_GENERATION
from agent_remote_server.repositories.ego_browser import EgoBrowserRepository
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserConnectedRequest,
    EgoBrowserDeviceRegisterRequest,
)
from agent_remote_server.security import hash_token
from agent_remote_server.services.ego_browser.contracts import (
    EXPLICIT_DIGEST,
    KNOWN_CAPABILITIES,
    LEGACY_NODE_CAPABILITY_FIELDS,
    NODE_ADMISSION_CAPABILITY_FIELDS,
    NODE_CAPABILITY_FIELDS,
    POLICY_CAPABILITIES,
    REQUIRED_CAPABILITIES,
    SAFE_DIGEST,
    SUPPORTED_CREDENTIAL_PROFILES,
    TERMINAL_STATUSES,
)
from agent_remote_server.services.ego_browser.helpers import (
    _as_utc,
    _canonical_capabilities,
    _decode_encryption_public_key,
    _decode_public_key,
    _record_revocation_metric,
    _safe_reason,
    _verify_pop,
)


class _EgoBrowserServiceBase:
    """
    保存 ego-browser 服务依赖并实现跨操作共享的不变量。
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_store: EgoBrowserRelayStore | None = None,
        revocation_publisher: EgoBrowserRevocationPublisher | None = None,
    ) -> None:
        """
        初始化Ego Browser 业务服务基础。

        :param session (AsyncSession): 会话
        :param settings (Settings): 配置
        :param relay_store (EgoBrowserRelayStore | None): 中继存储
        :param revocation_publisher (EgoBrowserRevocationPublisher | None): 撤销发布器
        """
        self._session = session
        self._settings = settings
        self._repository = EgoBrowserRepository(session)
        self._relay_store = relay_store
        self._revocation_publisher = revocation_publisher

    def _require_enrollment(self) -> None:
        """
        检查设备登记、状态查询和短期凭据刷新闸门。
        """

        if not self._settings.ego_browser_enrollment_enabled:
            self._error(
                "EGO_BROWSER_ENROLLMENT_DISABLED",
                "Ego-browser device enrollment is disabled.",
                503,
            )

    def _require_execution(self) -> None:
        """
        检查 claim、relay 和远端执行闸门。
        """

        if not self._settings.ego_browser_bridge_enabled:
            self._error(
                "EGO_BROWSER_EXECUTION_ADMISSION_DISABLED",
                "Ego-browser execution admission is disabled.",
                503,
            )

    def _require_enabled(self) -> None:
        """
        兼容旧调用方，将旧 bridge 开关解释为执行准入。
        """

        self._require_execution()

    def _validate_profile(
        self,
        *,
        release_profile: str,
        credential_profile: str,
        signer_certificate_sha256: str | None,
    ) -> None:
        """
        校验配置。

        :param release_profile (str): 发布配置
        :param credential_profile (str): 凭据配置
        :param signer_certificate_sha256 (str | None): 签名者 certificate sha256
        """
        expected = self._settings.ego_browser_expected_release_profile
        if (
            self._settings.environment.strip().lower() == "production"
            and release_profile != expected
        ):
            self._error(
                "EGO_BROWSER_PROFILE_MISMATCH",
                "The release profile is not accepted by this server.",
                409,
            )
        if credential_profile not in SUPPORTED_CREDENTIAL_PROFILES:
            self._error(
                "EGO_BROWSER_CREDENTIAL_PROFILE_UNSUPPORTED",
                "The requested credential storage profile is not implemented by this client.",
                409,
            )
        pinned = self._settings.ego_browser_expected_signer_certificate_sha256
        if pinned and signer_certificate_sha256 != pinned:
            self._error(
                "EGO_BROWSER_SIGNER_MISMATCH",
                "The signer certificate is not pinned by this server.",
                409,
            )
        # 开发占位值仅限隔离测试；社区和开发者发布必须提供真实的小写 SHA-256 指纹。
        signer_is_hex = (
            isinstance(signer_certificate_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", signer_certificate_sha256) is not None
        )
        if release_profile in {"community-local-trust", "developer-id"}:
            if not signer_is_hex:
                self._error(
                    "EGO_BROWSER_SIGNER_INVALID",
                    "A production release profile requires a lowercase 64-character signer digest.",
                    422,
                )
        elif release_profile in {"logic-test", "development-local"}:
            if signer_certificate_sha256 != "development" and not signer_is_hex:
                self._error(
                    "EGO_BROWSER_SIGNER_INVALID",
                    "The development signer must be the approved sentinel or a lowercase digest.",
                    422,
                )
        else:
            self._error(
                "EGO_BROWSER_PROFILE_MISMATCH",
                "The release profile is not accepted by this server.",
                409,
            )

    def _validate_device_origin(self, device: EgoBrowserDevice, *, bind_legacy: bool = True) -> str:
        """
        校验设备固定的 Server origin，并为旧记录执行一次性绑定。

        :param device (EgoBrowserDevice): 待校验的设备
        :param bind_legacy (bool): 是否将旧 null origin 绑定到当前 origin
        :return str: 当前规范 Server origin
        """

        current_origin = self._settings.public_origin
        stored_origin = getattr(device, "server_origin", None)
        if stored_origin is None:
            if bind_legacy:
                self._bind_legacy_device_origin(device)
            return current_origin
        if stored_origin != current_origin:
            self._error(
                "EGO_BROWSER_IDENTITY_ORIGIN_CONFLICT",
                "The device identity belongs to a different Server origin.",
                409,
            )
        return stored_origin

    def _bind_legacy_device_origin(self, device: EgoBrowserDevice) -> bool:
        """
        在一次成功的认证操作中绑定旧设备的 Server origin。

        :param device (EgoBrowserDevice): 待绑定的独立设备
        :return bool: 是否刚刚写入了 origin
        """

        if getattr(device, "server_origin", None) is not None:
            return False
        device.server_origin = self._settings.public_origin
        return True

    def _binding_profile_is_current(
        self, binding: EgoBrowserBinding, device: EgoBrowserDevice
    ) -> bool:
        """
        判断已持久化的发布身份是否仍符合当前策略。

        :param binding (EgoBrowserBinding): 绑定
        :param device (EgoBrowserDevice): 设备
        :return bool: 是否满足校验条件
        """

        # 布尔准入路径遇到损坏元数据必须返回 False；旧来源绑定只在认证调用方中执行。
        try:
            expected = self._settings.ego_browser_expected_release_profile
            production = self._settings.environment.strip().lower() == "production"
            pinned = self._settings.ego_browser_expected_signer_certificate_sha256
            stored_origin = getattr(device, "server_origin", None)
            production_profiles = {"community-local-trust", "developer-id"}
            if (
                device.status != "active"
                or (stored_origin is not None and stored_origin != self._settings.public_origin)
                or device.platform != "macos"
                or binding.release_profile != device.release_profile
                or binding.signer_certificate_sha256 != device.signer_certificate_sha256
                or binding.credential_profile != device.credential_profile
                or binding.control_channel != "ego_browser_bridge"
                or binding.relay_binding_kind != "ego_browser"
                or binding.authorization_mode != "ego_browser_script_full_trust"
                or binding.authorization_policy_version != 1
                or device.credential_profile not in SUPPORTED_CREDENTIAL_PROFILES
                or (
                    not production
                    and device.release_profile
                    not in {
                        "logic-test",
                        "development-local",
                        "community-local-trust",
                        "developer-id",
                    }
                )
                or (production and device.release_profile != expected)
                or (pinned and device.signer_certificate_sha256 != pinned)
                or device.bridge_protocol_version != EGO_BROWSER_PROTOCOL
                or device.bridge_protocol_version
                != self._settings.ego_browser_expected_protocol_version
                or binding.bridge_protocol_version != device.bridge_protocol_version
                or binding.local_runtime_version != device.local_ego_browser_runtime_version
                or binding.ego_lite_runtime_version != device.ego_lite_runtime_version
                or binding.skill_version != device.skill_version
                or binding.allowlist_revision != device.allowlist_revision
                or binding.allowlist_roots_digest != device.allowlist_roots_digest
                or binding.learning_bundle_digest != device.learning_bundle_digest
                or binding.remote_platform != "linux"
                or binding.local_platform != "macos"
                or (
                    device.release_profile in production_profiles
                    and (
                        (
                            device.local_ego_browser_runtime_version is not None
                            and device.local_ego_browser_runtime_version
                            != self._settings.ego_browser_expected_local_runtime_version
                        )
                        or (
                            device.ego_lite_runtime_version is not None
                            and device.ego_lite_runtime_version
                            != self._settings.ego_browser_expected_local_runtime_version
                        )
                        or (
                            device.skill_version is not None
                            and device.skill_version
                            != self._settings.ego_browser_expected_skill_version
                        )
                    )
                )
            ):
                return False
            if (
                isinstance(device.allowlist_revision, bool)
                or not isinstance(device.allowlist_revision, int)
                or device.allowlist_revision < 1
                or isinstance(binding.allowlist_revision, bool)
                or not isinstance(binding.allowlist_revision, int)
                or binding.allowlist_revision < 1
            ):
                return False
            for digest in (device.allowlist_roots_digest, device.learning_bundle_digest):
                if digest is not None and SAFE_DIGEST.fullmatch(digest) is None:
                    return False
            if not isinstance(device.capabilities, list) or not isinstance(
                binding.capabilities, list
            ):
                return False
            canonical_capabilities = _canonical_capabilities(device.capabilities)
            if len(device.capabilities) != len(canonical_capabilities):
                return False
            canonical_binding_capabilities = _canonical_capabilities(binding.capabilities)
            if len(binding.capabilities) != len(canonical_binding_capabilities):
                return False
            self._validate_profile(
                release_profile=device.release_profile,
                credential_profile=device.credential_profile,
                signer_certificate_sha256=device.signer_certificate_sha256,
            )
            self._validate_policy_capabilities(
                canonical_capabilities,
                allowlist_roots_digest=device.allowlist_roots_digest,
                learning_bundle_digest=device.learning_bundle_digest,
            )
        except (ApiError, AttributeError, TypeError, ValueError, OverflowError):
            return False
        return canonical_binding_capabilities == canonical_capabilities

    def _validate_binding_profile(
        self, binding: EgoBrowserBinding, device: EgoBrowserDevice
    ) -> None:
        """
        当 live binding 的发布身份漂移时按拒绝策略处理。

        :param binding (EgoBrowserBinding): 绑定
        :param device (EgoBrowserDevice): 设备
        """

        # 谓词不得提前持久化旧来源；所属操作在全部校验成功后再绑定并提交。
        self._validate_device_origin(device, bind_legacy=False)
        self._validate_profile(
            release_profile=device.release_profile,
            credential_profile=device.credential_profile,
            signer_certificate_sha256=device.signer_certificate_sha256,
        )
        if not self._binding_profile_is_current(binding, device):
            self._error(
                "EGO_BROWSER_PROFILE_MISMATCH",
                "The binding release identity no longer matches the registered device.",
                409,
            )

    def _validate_node_capability(self, node: Node, *, runtime_backend: str | None = None) -> None:
        """
        校验节点能力。

        :param node (Node): 节点
        :param runtime_backend (str | None): 运行时后端
        """
        capabilities = node.runtime_capabilities
        raw = capabilities.get("ego_browser_bridge")
        if not isinstance(raw, dict) or raw.get("supported") is not True:
            self._error(
                "EGO_BROWSER_NODE_UNAVAILABLE",
                "The assigned node has no complete ego-browser artifact capability.",
                409,
            )
        fields = set(raw)
        if fields not in {LEGACY_NODE_CAPABILITY_FIELDS, NODE_CAPABILITY_FIELDS}:
            self._error(
                "EGO_BROWSER_NODE_UNAVAILABLE",
                "The assigned node has no complete ego-browser artifact capability.",
                409,
            )
        # 新客户端必须证明管理员意图及两项独立准入；旧载荷仅保留一个兼容窗口。
        if fields == NODE_CAPABILITY_FIELDS:
            for field in NODE_ADMISSION_CAPABILITY_FIELDS:
                if not isinstance(raw.get(field), bool):
                    self._error(
                        "EGO_BROWSER_CAPABILITY_MISMATCH",
                        "The node ego-browser admission fields are invalid.",
                        409,
                    )
            if (
                raw.get("configured_enabled") is not True
                or raw.get("effective_enabled") is not True
                or raw.get("node_execution_allowed") is not True
            ):
                self._error(
                    "EGO_BROWSER_NODE_UNAVAILABLE",
                    "The assigned node is not admitted for ego-browser execution.",
                    409,
                )
            if raw.get("effective_enabled") is True and raw.get("configured_enabled") is not True:
                self._error(
                    "EGO_BROWSER_NODE_UNAVAILABLE",
                    "The node effective capability contradicts its configured intent.",
                    409,
                )
            if (
                raw.get("node_execution_allowed") is True
                and raw.get("effective_enabled") is not True
            ):
                self._error(
                    "EGO_BROWSER_NODE_UNAVAILABLE",
                    "The node execution admission contradicts its effective capability.",
                    409,
                )
        versions = raw.get("protocol_versions")
        if versions != [EGO_BROWSER_PROTOCOL]:
            self._error(
                "EGO_BROWSER_VERSION_MISMATCH", "The node wrapper protocol is incompatible.", 409
            )
        backends = raw.get("backends")
        if (
            not isinstance(backends, list)
            or not backends
            or len(backends) != len(set(backends))
            or any(backend not in {"docker_sandbox", "native"} for backend in backends)
            or (runtime_backend is not None and runtime_backend not in backends)
        ):
            self._error(
                "EGO_BROWSER_RUNTIME_UNSUPPORTED",
                "The node does not support the selected runtime backend for ego-browser.",
                409,
            )
        if (
            raw.get("wrapper_version") != self._settings.ego_browser_expected_wrapper_version
            or raw.get("skill_version") != self._settings.ego_browser_expected_skill_version
            or raw.get("skill_tree_sha256") != self._settings.ego_browser_expected_skill_tree_sha256
            or raw.get("remote_platform") != "linux"
            or raw.get("local_platform") != "macos"
        ):
            self._error(
                "EGO_BROWSER_VERSION_MISMATCH",
                "The node wrapper or official Skill artifact is incompatible.",
                409,
            )

        max_script_bytes = raw.get("max_script_bytes")
        max_execute_timeout_ms = raw.get("max_execute_timeout_ms")
        if (
            not isinstance(max_script_bytes, int)
            or isinstance(max_script_bytes, bool)
            or max_script_bytes < 1
            or max_script_bytes > 1_048_576
            or not isinstance(max_execute_timeout_ms, int)
            or isinstance(max_execute_timeout_ms, bool)
            or max_execute_timeout_ms < 1
            or max_execute_timeout_ms > 120_000
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The node ego-browser execution limits are invalid.",
                409,
            )

    def _node_supports_backend(self, node: Node, runtime_backend: str) -> bool:
        """
        判断节点是否支持指定浏览器后端。

        :param node (Node): 节点
        :param runtime_backend (str): 运行时后端
        :return bool: 是否满足校验条件
        """
        try:
            self._validate_node_capability(node, runtime_backend=runtime_backend)
        except ApiError:
            return False
        return True

    def _validate_capability_payload(
        self, device: EgoBrowserDevice, payload: EgoBrowserConnectedRequest
    ) -> None:
        """
        校验能力载荷。

        :param device (EgoBrowserDevice): 设备
        :param payload (EgoBrowserConnectedRequest): 载荷
        """
        if device.encryption_public_key is None:
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_REQUIRED",
                "The device has no registered encryption key.",
                409,
            )
        registered_encryption_key = _decode_encryption_public_key(device.encryption_public_key)
        connected_encryption_key = _decode_encryption_public_key(payload.encryption_public_key)
        if (
            connected_encryption_key is None
            or connected_encryption_key != registered_encryption_key
        ):
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_MISMATCH",
                "The Bridge encryption key does not match registration.",
                409,
            )
        if payload.bridge_protocol_version != EGO_BROWSER_PROTOCOL:
            self._error("EGO_BROWSER_VERSION_MISMATCH", "The Bridge protocol is incompatible.", 409)
        if (
            payload.release_profile != device.release_profile
            or payload.credential_profile != device.credential_profile
        ):
            self._error(
                "EGO_BROWSER_PROFILE_MISMATCH",
                "The Bridge profile does not match registration.",
                409,
            )
        if payload.signer_certificate_sha256 != device.signer_certificate_sha256:
            self._error(
                "EGO_BROWSER_SIGNER_MISMATCH", "The Bridge signer does not match registration.", 409
            )
        if (
            payload.allowlist_revision != device.allowlist_revision
            or payload.allowlist_roots_digest != device.allowlist_roots_digest
            or payload.learning_bundle_digest != device.learning_bundle_digest
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The Bridge capability revision does not match.",
                409,
            )
        self._validate_policy_capabilities(
            payload.capabilities,
            allowlist_roots_digest=payload.allowlist_roots_digest,
            learning_bundle_digest=payload.learning_bundle_digest,
        )
        if payload.max_parallel_requests > self._settings.ego_browser_max_parallel_requests:
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH", "The Bridge parallelism exceeds policy.", 409
            )

    def _validate_policy_capabilities(
        self,
        values: Iterable[str],
        *,
        allowlist_roots_digest: str | None,
        learning_bundle_digest: str | None,
    ) -> list[str]:
        """
        校验能力集合与本地已验证策略资源逐项一致。

        :param values (Iterable[str]): 待处理值
        :param allowlist_roots_digest (str | None): 允许列表 roots 摘要
        :param learning_bundle_digest (str | None): 学习包摘要
        :return list[str]: 策略能力
        """

        supplied = list(values)
        capabilities = _canonical_capabilities(supplied)
        if len(supplied) != len(capabilities) or any(
            capability not in KNOWN_CAPABILITIES for capability in capabilities
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The Bridge capability set contains duplicates or unknown values.",
                409,
            )
        if not set(REQUIRED_CAPABILITIES).issubset(capabilities):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The Bridge capability set is incomplete.",
                409,
            )
        has_allowlist = "ego_browser_file_allowlist_v1" in capabilities
        has_learning = "ego_browser_site_learning_v1" in capabilities
        if has_allowlist != (allowlist_roots_digest is not None) or has_learning != (
            learning_bundle_digest is not None
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "Policy-backed capabilities do not match their verified digests.",
                409,
            )
        if self._settings.environment.strip().lower() == "production" and not set(
            POLICY_CAPABILITIES
        ).issubset(capabilities):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "Production requires verified allowlist and Site Learning capabilities.",
                409,
            )
        return capabilities

    def _validate_policy_digests(
        self,
        *,
        policy_digest: str | None,
        capability_digest: str | None,
        allowlist_revision: int,
        allowlist_roots_digest: str | None,
        learning_bundle_digest: str | None,
        capabilities: Iterable[str],
    ) -> None:
        """
        校验策略 digests。

        :param policy_digest (str | None): 策略摘要
        :param capability_digest (str | None): 能力摘要
        :param allowlist_revision (int): 允许列表版本
        :param allowlist_roots_digest (str | None): 允许列表 roots 摘要
        :param learning_bundle_digest (str | None): 学习包摘要
        :param capabilities (Iterable[str]): 能力
        """

        policy_material = json.dumps(
            {
                "allowlist_revision": allowlist_revision,
                "allowlist_roots_digest": allowlist_roots_digest,
                "learning_bundle_digest": learning_bundle_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        expected_policy = hashlib.sha256(policy_material).hexdigest()
        canonical_capabilities = "\n".join(sorted(set(capabilities))).encode()
        expected_capability = hashlib.sha256(canonical_capabilities).hexdigest()
        if policy_digest is not None and EXPLICIT_DIGEST.fullmatch(policy_digest) is None:
            self._error(
                "EGO_BROWSER_DIGEST_INVALID",
                "The Bridge policy digest format is invalid.",
                422,
            )
        if policy_digest is not None and policy_digest.removeprefix("sha256:") != expected_policy:
            self._error(
                "EGO_BROWSER_POLICY_DIGEST_MISMATCH",
                "The Bridge policy digest does not match its declared policy.",
                409,
            )
        if capability_digest is not None and EXPLICIT_DIGEST.fullmatch(capability_digest) is None:
            self._error(
                "EGO_BROWSER_DIGEST_INVALID",
                "The Bridge capability digest format is invalid.",
                422,
            )
        if (
            capability_digest is not None
            and capability_digest.removeprefix("sha256:") != expected_capability
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_DIGEST_MISMATCH",
                "The Bridge capability digest does not match its declared capabilities.",
                409,
            )

    async def _validate_device_pop(
        self,
        *,
        device: EgoBrowserDevice,
        operation_generation: int,
        operation: str,
        binding_id: UUID | None,
        payload: BaseModel,
    ) -> None:
        """
        校验设备对完整操作 payload 的单次签名。

        :param device (EgoBrowserDevice): 设备
        :param operation_generation (int): 操作代次
        :param operation (str): 操作
        :param binding_id (UUID | None): 绑定 ID
        :param payload (BaseModel): 载荷
        """

        await self._validate_pop(
            public_key=_decode_public_key(device.public_key),
            user_id=device.user_id,
            device_id=device.id,
            device_generation=device.generation,
            operation_generation=operation_generation,
            release_profile=device.release_profile,
            credential_profile=device.credential_profile,
            operation=operation,
            binding_id=binding_id,
            payload=payload,
        )

    async def _validate_pop(
        self,
        *,
        public_key: bytes,
        user_id: UUID,
        device_id: UUID,
        device_generation: int,
        operation_generation: int,
        release_profile: str,
        credential_profile: str,
        operation: str,
        binding_id: UUID | None,
        payload: BaseModel,
    ) -> None:
        """
        校验 payload-bound PoP 并原子消费服务端 challenge。

        :param public_key (bytes): 公钥
        :param user_id (UUID): 用户 ID
        :param device_id (UUID): 设备 ID
        :param device_generation (int): 设备代次
        :param operation_generation (int): 操作代次
        :param release_profile (str): 发布配置
        :param credential_profile (str): 凭据配置
        :param operation (str): 操作
        :param binding_id (UUID | None): 绑定 ID
        :param payload (BaseModel): 载荷
        """

        if not self._settings.ego_browser_require_device_pop:
            return
        challenge = getattr(payload, "proof_challenge", None)
        signature = getattr(payload, "proof_signature", None)
        if not challenge or not signature:
            self._error(
                "EGO_BROWSER_POP_REQUIRED",
                "Device proof-of-possession is required.",
                403,
            )
        if not _verify_pop(
            public_key=public_key,
            challenge=challenge,
            signature=signature,
            device_id=device_id,
            device_generation=device_generation,
            operation_generation=operation_generation,
            release_profile=release_profile,
            credential_profile=credential_profile,
            server_host=self._server_host(),
            operation=operation,
            binding_id=binding_id,
            payload=payload,
        ):
            self._error(
                "EGO_BROWSER_POP_INVALID",
                "Device proof-of-possession is invalid.",
                403,
            )
        if self._relay_store is None:
            self._error(
                "EGO_BROWSER_POP_UNAVAILABLE",
                "The proof-of-possession challenge store is unavailable.",
                503,
            )
        claims = await self._relay_store.consume_proof_challenge(
            token_hash=hash_token(self._settings.secret_key, challenge)
        )
        if claims != EgoBrowserProofChallengeClaims(
            user_id=user_id,
            ego_browser_device_id=device_id,
            operation=operation,
            generation=operation_generation,
            binding_id=binding_id,
        ):
            self._error(
                "EGO_BROWSER_POP_CHALLENGE_INVALID",
                "The proof-of-possession challenge is expired, replayed, or mismatched.",
                403,
            )

    async def _owned_binding_device(
        self,
        user: User,
        binding_id: UUID,
        device_id: UUID | None,
        *,
        for_update: bool,
        allow_admin: bool = False,
    ) -> tuple[EgoBrowserBinding, EgoBrowserDevice]:
        """
        返回owned 绑定设备。

        :param user (User): 用户
        :param binding_id (UUID): 绑定 ID
        :param device_id (UUID | None): 设备 ID
        :param for_update (bool): 对应更新
        :param allow_admin (bool): allow 管理员
        :return tuple[EgoBrowserBinding, EgoBrowserDevice]: owned 绑定设备
        """
        binding = await self._repository.get_binding(binding_id, for_update=for_update)
        if binding is None or (
            binding.user_id != user.id and not (allow_admin and user.role == "admin")
        ):
            self._error("EGO_BROWSER_BINDING_NOT_FOUND", "The browser binding was not found.", 404)
        if device_id is not None and binding.ego_browser_device_id != device_id:
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND", "The ego-browser device was not found.", 404
            )
        device = await self._repository.get_device(
            binding.ego_browser_device_id, for_update=for_update
        )
        if device is None or device.user_id != binding.user_id:
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND", "The ego-browser device was not found.", 404
            )
        return binding, device

    def _update_device_metadata(
        self,
        device: EgoBrowserDevice,
        payload: EgoBrowserDeviceRegisterRequest,
        canonical_encryption_key: str | None = None,
    ) -> None:
        """
        更新设备元数据。

        :param device (EgoBrowserDevice): 设备
        :param payload (EgoBrowserDeviceRegisterRequest): 载荷
        :param canonical_encryption_key (str | None): canonical encryption 键
        """
        device.release_profile = payload.release_profile
        device.signer_certificate_sha256 = payload.signer_certificate_sha256
        device.credential_profile = payload.credential_profile
        device.bridge_protocol_version = payload.bridge_protocol_version
        device.bridge_version = payload.bridge_version
        device.local_ego_browser_runtime_version = payload.local_ego_browser_runtime_version
        device.ego_lite_runtime_version = payload.ego_lite_runtime_version
        device.skill_version = payload.skill_version
        device.capabilities = _canonical_capabilities(payload.capabilities)
        device.allowlist_revision = payload.allowlist_revision
        device.allowlist_roots_digest = payload.allowlist_roots_digest
        device.learning_bundle_digest = payload.learning_bundle_digest
        if canonical_encryption_key is not None:
            device.encryption_public_key = canonical_encryption_key

    def _update_device_from_connected(
        self, device: EgoBrowserDevice, payload: EgoBrowserConnectedRequest
    ) -> None:
        """
        更新设备来自已连接。

        :param device (EgoBrowserDevice): 设备
        :param payload (EgoBrowserConnectedRequest): 载荷
        """
        device.bridge_protocol_version = payload.bridge_protocol_version
        device.bridge_version = payload.bridge_version
        device.local_ego_browser_runtime_version = payload.local_ego_browser_runtime_version
        device.ego_lite_runtime_version = payload.ego_lite_runtime_version
        device.skill_version = payload.skill_version
        device.capabilities = _canonical_capabilities(payload.capabilities)

    def _advance_generation(self, binding: EgoBrowserBinding) -> None:
        """
        推进代次。

        :param binding (EgoBrowserBinding): 绑定
        """
        if binding.generation >= MAX_ACTIVE_EGO_BROWSER_GENERATION:
            self._error(
                "EGO_BROWSER_GENERATION_EXHAUSTED", "The binding generation is exhausted.", 409
            )
        binding.generation += 1

    async def _invalidate_bindings(
        self,
        bindings: Iterable[EgoBrowserBinding],
        *,
        terminal_status: Literal["stopped", "expired", "revoked"],
        reason: str,
        actor_user_id: UUID | None,
        now: datetime | None = None,
    ) -> list[tuple[UUID, int]]:
        """
        用同一事务步骤使一组 binding generation 失效。

        :param bindings (Iterable[EgoBrowserBinding]): 绑定
        :param terminal_status (Literal["stopped", "expired", "revoked"]): 终态状态
        :param reason (str): 操作原因
        :param actor_user_id (UUID | None): actor 用户 ID
        :param now (datetime | None): 当前时间
        :return list[tuple[UUID, int]]: invalidate 绑定
        """

        timestamp = now or self._now()
        safe_reason = _safe_reason(reason)
        invalidated: list[tuple[UUID, int]] = []
        for binding in bindings:
            if binding.status in TERMINAL_STATUSES and (
                terminal_status != "revoked" or binding.status == "revoked"
            ):
                continue
            old_generation = binding.generation
            await self._terminalize_generation_requests(
                binding=binding,
                generation=old_generation,
                reason=safe_reason,
                actor_user_id=actor_user_id,
            )
            self._advance_generation(binding)
            binding.status = terminal_status
            binding.lease_until = None
            binding.lease_health = "expired"
            binding.lease_grace_until = None
            binding.stopped_at = timestamp
            binding.revoked_at = timestamp if terminal_status == "revoked" else None
            binding.stop_reason = safe_reason
            await self._enqueue_revocation(binding, old_generation, safe_reason)
            await self._audit(
                actor_user_id,
                (
                    "ego_browser_binding.stopped"
                    if terminal_status == "stopped"
                    else "ego_browser_binding.revoked"
                ),
                str(binding.id),
                self._binding_details(binding),
            )
            invalidated.append((binding.id, old_generation))
        return invalidated

    async def _terminalize_generation_requests(
        self,
        *,
        binding: EgoBrowserBinding,
        generation: int,
        reason: str,
        actor_user_id: UUID | None,
    ) -> None:
        """
        在撤销 generation 的同一事务中终结其活动请求。

        :param binding (EgoBrowserBinding): 绑定
        :param generation (int): 代次
        :param reason (str): 操作原因
        :param actor_user_id (UUID | None): actor 用户 ID
        """

        requests = await self._repository.cancel_generation_requests(
            binding_id=binding.id,
            generation=generation,
        )
        for request in requests:
            await self._audit(
                actor_user_id,
                "ego_browser_execute.cancelled",
                str(binding.id),
                {
                    "generation": generation,
                    "request_id": request.request_id,
                    "sequence": request.sequence,
                    "reason": _safe_reason(reason),
                },
            )

    async def _enqueue_revocation(
        self, binding: EgoBrowserBinding, generation: int, reason: str
    ) -> None:
        """
        将其加入队列撤销。

        :param binding (EgoBrowserBinding): 绑定
        :param generation (int): 代次
        :param reason (str): 操作原因
        """
        event = EgoBrowserRevocationOutbox(
            binding_id=binding.id,
            generation=generation,
            reason=_safe_reason(reason),
        )
        try:
            async with self._session.begin_nested():
                await self._repository.add_outbox(event)
        except IntegrityError:
            # 重复生命周期回调必须幂等，因此保留调用方外层事务而不执行回滚。
            return

    async def _expire_binding(
        self, binding: EgoBrowserBinding, *, reason: str, commit: bool
    ) -> None:
        """
        过期处理绑定。

        :param binding (EgoBrowserBinding): 绑定
        :param reason (str): 操作原因
        :param commit (bool): 是否立即提交事务
        """
        invalidated = await self._invalidate_bindings(
            [binding],
            terminal_status="expired",
            reason=reason,
            actor_user_id=None,
        )
        if commit and invalidated:
            await self._session.commit()
            await self._publish_revocation(*invalidated[0])

    async def _reject_expired_renewal(self, binding: EgoBrowserBinding, now: datetime) -> None:
        """
        拒绝并撤销已经越过当前租约或宽限截止时间的续租。

        :param binding (EgoBrowserBinding): 绑定
        :param now (datetime): 当前时间
        """

        reason: str | None = None
        lease_until = binding.lease_until
        if now >= _as_utc(binding.absolute_ttl_until):
            reason = "absolute_ttl"
        elif binding.lease_health == "healthy" and lease_until is None:
            reason = "lease_expired"
        elif (
            binding.lease_health == "healthy"
            and lease_until is not None
            and _as_utc(lease_until) <= now
        ):
            grace_until = min(
                _as_utc(lease_until)
                + timedelta(seconds=self._settings.ego_browser_lease_renew_failure_grace_seconds),
                _as_utc(binding.absolute_ttl_until),
            )
            if now < grace_until:
                binding.lease_health = "renewal_grace"
                binding.lease_grace_until = grace_until
                await self._audit(
                    None,
                    "ego_browser_binding.renewal_failed",
                    str(binding.id),
                    self._binding_details(binding),
                )
                return
            reason = "renewal_grace_expired"
        elif binding.lease_health == "renewal_grace" and (
            binding.lease_grace_until is None or _as_utc(binding.lease_grace_until) <= now
        ):
            reason = "renewal_grace_expired"
        if reason is None:
            return

        old_generation = binding.generation
        await self._expire_binding(binding, reason=reason, commit=False)
        await self._session.commit()
        await self._publish_revocation(binding.id, old_generation)
        self._error("EGO_BROWSER_LEASE_EXPIRED", "The browser binding lease has expired.", 409)

    async def _publish_revocation(self, binding_id: UUID, generation: int) -> None:
        """
        发布撤销。

        :param binding_id (UUID): 绑定 ID
        :param generation (int): 代次
        """
        publisher = self._revocation_publisher
        if publisher is None:
            return
        event = await self._repository.get_outbox(
            binding_id=binding_id,
            generation=generation,
            for_update=True,
        )
        if event is None or event.delivered_at is not None:
            return
        publish = getattr(publisher, "publish", None)
        if publish is None:
            return
        try:
            await publish(binding_id, generation)
        except Exception:
            # 保留持久化待发布状态，由清理 worker 稍后重试。
            return
        event.delivered_at = self._now()
        _record_revocation_metric(event.reason)
        await self._session.commit()

    async def _audit(
        self,
        actor_user_id: UUID | None,
        action: str,
        target_id: str,
        details: dict[str, object],
    ) -> None:
        """
        读取审计记录。

        :param actor_user_id (UUID | None): actor 用户 ID
        :param action (str): 操作
        :param target_id (str): 审计目标 ID
        :param details (dict[str, object]): 详情
        """
        self._session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type="ego_browser_binding"
                if "binding" in action or "allowlist" in action
                else "ego_browser_device",
                target_id=target_id,
                details=details,
            )
        )
        await self._session.flush()

    @staticmethod
    def _binding_details(binding: EgoBrowserBinding) -> dict[str, object]:
        """
        返回绑定详情。

        :param binding (EgoBrowserBinding): 绑定
        :return dict[str, object]: 绑定详情
        """
        return {
            "binding_id": str(binding.id),
            "device_id": str(binding.ego_browser_device_id),
            "tool_session_id": str(binding.binding_tool_session_id),
            "node_id": str(binding.node_id),
            "generation": binding.generation,
            "status": binding.status,
            "release_profile": binding.release_profile,
            "credential_profile": binding.credential_profile,
            "allowlist_revision": binding.allowlist_revision,
            "learning_bundle_digest": binding.learning_bundle_digest,
            "concurrency_mode": binding.concurrency_mode,
        }

    def _server_host(self) -> str:
        """
        返回服务端主机。

        :return str: 服务端主机
        """
        return urlparse(self._settings.public_base_url).hostname or "localhost"

    async def expire_due(self) -> int:
        """
        过期所有达到租约或绝对 TTL 的 live binding。

        :return int: 本次转为终态的过期 binding 数量
        """
        now = self._now()
        bindings = list(await self._repository.list_due_bindings(now, for_update=True))
        revoked_generations: list[tuple[UUID, int]] = []
        for binding in bindings:
            absolute_due = now >= _as_utc(binding.absolute_ttl_until)
            if (
                not absolute_due
                and binding.lease_health == "healthy"
                and binding.lease_until is not None
            ):
                grace_until = min(
                    _as_utc(binding.lease_until)
                    + timedelta(
                        seconds=self._settings.ego_browser_lease_renew_failure_grace_seconds
                    ),
                    _as_utc(binding.absolute_ttl_until),
                )
                if now < grace_until:
                    binding.lease_health = "renewal_grace"
                    binding.lease_grace_until = grace_until
                    await self._audit(
                        None,
                        "ego_browser_binding.renewal_failed",
                        str(binding.id),
                        self._binding_details(binding),
                    )
                    continue
            reason = "absolute_ttl" if absolute_due else "renewal_grace_expired"
            revoked_generations.extend(
                await self._invalidate_bindings(
                    [binding],
                    terminal_status="expired",
                    reason=reason,
                    actor_user_id=None,
                    now=now,
                )
            )
        if bindings:
            await self._session.commit()
            for binding_id, generation in revoked_generations:
                await self._publish_revocation(binding_id, generation)
        return len(revoked_generations)

    async def publish_pending_revocations(self, *, limit: int | None = None) -> int:
        """
        发布并标记已提交的撤销 outbox 事件。

        :param limit (int | None): 单次处理的最大记录数；None 表示使用配置值
        :return int: 本次成功投递的撤销事件数量
        """

        if self._revocation_publisher is None:
            return 0
        batch_size = limit or self._settings.ego_browser_cleanup_batch_size
        events = list(await self._repository.list_pending_outbox(limit=batch_size, for_update=True))
        if not events:
            return 0
        published = 0
        for event in events:
            try:
                await self._revocation_publisher.publish(event.binding_id, event.generation)
            except Exception:
                # 保留待发布记录，由后续 worker 迭代重试。
                continue
            event.delivered_at = self._now()
            _record_revocation_metric(event.reason)
            published += 1
        if published:
            await self._session.commit()
        return published

    @staticmethod
    def _now() -> datetime:
        """
        获取当前时间。

        :return datetime: 当前时间
        """
        return datetime.now(UTC)

    @staticmethod
    def _error(code: str, message: str, status_code: int) -> NoReturn:
        """
        构造 API 错误。

        :param code (str): 代码
        :param message (str): 消息内容
        :param status_code (int): 状态代码
        """
        raise ApiError(code=code, message=message, status_code=status_code)

    def _generation_error(self) -> NoReturn:
        """
        构造设备代次冲突错误。
        """
        self._error("EGO_BROWSER_GENERATION_MISMATCH", "The binding generation is stale.", 409)
