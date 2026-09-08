"""封装 ego-browser 服务共享的状态、校验与事务操作。"""

from __future__ import annotations

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
    KNOWN_CAPABILITIES,
    NODE_CAPABILITY_FIELDS,
    POLICY_CAPABILITIES,
    REQUIRED_CAPABILITIES,
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
    """保存 ego-browser 服务依赖并实现跨操作共享的不变量。"""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_store: EgoBrowserRelayStore | None = None,
        revocation_publisher: EgoBrowserRevocationPublisher | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._repository = EgoBrowserRepository(session)
        self._relay_store = relay_store
        self._revocation_publisher = revocation_publisher

    def _require_enabled(self) -> None:
        if not self._settings.ego_browser_bridge_enabled:
            self._error("EGO_BROWSER_BRIDGE_DISABLED", "The ego-browser bridge is disabled.", 503)

    def _validate_profile(
        self,
        *,
        release_profile: str,
        credential_profile: str,
        signer_certificate_sha256: str | None,
    ) -> None:
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
        if credential_profile not in {"community_file", "keychain_access_group"}:
            self._error(
                "EGO_BROWSER_PROFILE_MISMATCH", "The credential profile is not accepted.", 409
            )
        pinned = self._settings.ego_browser_expected_signer_certificate_sha256
        if pinned and signer_certificate_sha256 != pinned:
            self._error(
                "EGO_BROWSER_SIGNER_MISMATCH",
                "The signer certificate is not pinned by this server.",
                409,
            )

    def _binding_profile_is_current(
        self, binding: EgoBrowserBinding, device: EgoBrowserDevice
    ) -> bool:
        """判断已持久化的发布身份是否仍符合当前策略。"""

        expected = self._settings.ego_browser_expected_release_profile
        production = self._settings.environment.strip().lower() == "production"
        pinned = self._settings.ego_browser_expected_signer_certificate_sha256
        return (
            device.status == "active"
            and binding.release_profile == device.release_profile
            and binding.signer_certificate_sha256 == device.signer_certificate_sha256
            and binding.credential_profile == device.credential_profile
            and device.credential_profile in {"community_file", "keychain_access_group"}
            and (not production or device.release_profile == expected)
            and (not pinned or device.signer_certificate_sha256 == pinned)
        )

    def _validate_binding_profile(
        self, binding: EgoBrowserBinding, device: EgoBrowserDevice
    ) -> None:
        """当 live binding 的发布身份漂移时按拒绝策略处理。"""

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
        capabilities = node.runtime_capabilities
        raw = capabilities.get("ego_browser_bridge")
        if (
            not isinstance(raw, dict)
            or raw.get("supported") is not True
            or set(raw) != NODE_CAPABILITY_FIELDS
        ):
            self._error(
                "EGO_BROWSER_NODE_UNAVAILABLE",
                "The assigned node has no complete ego-browser artifact capability.",
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
        try:
            self._validate_node_capability(node, runtime_backend=runtime_backend)
        except ApiError:
            return False
        return True

    def _validate_capability_payload(
        self, device: EgoBrowserDevice, payload: EgoBrowserConnectedRequest
    ) -> None:
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
        """校验能力集合与本地已验证策略资源逐项一致。"""

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

    async def _validate_device_pop(
        self,
        *,
        device: EgoBrowserDevice,
        operation_generation: int,
        operation: str,
        binding_id: UUID | None,
        payload: BaseModel,
    ) -> None:
        """校验设备对完整操作 payload 的单次签名。"""

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
        """校验 payload-bound PoP 并原子消费服务端 challenge。"""

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
        device.bridge_protocol_version = payload.bridge_protocol_version
        device.bridge_version = payload.bridge_version
        device.local_ego_browser_runtime_version = payload.local_ego_browser_runtime_version
        device.ego_lite_runtime_version = payload.ego_lite_runtime_version
        device.skill_version = payload.skill_version
        device.capabilities = _canonical_capabilities(payload.capabilities)

    def _advance_generation(self, binding: EgoBrowserBinding) -> None:
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
        """用同一事务步骤使一组 binding generation 失效。"""

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
        """在撤销 generation 的同一事务中终结其活动请求。"""

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
        """拒绝并撤销已经越过当前租约或宽限截止时间的续租。"""

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
        return datetime.now(UTC)

    @staticmethod
    def _error(code: str, message: str, status_code: int) -> NoReturn:
        raise ApiError(code=code, message=message, status_code=status_code)

    def _generation_error(self) -> NoReturn:
        self._error("EGO_BROWSER_GENERATION_MISMATCH", "The binding generation is stale.", 409)
