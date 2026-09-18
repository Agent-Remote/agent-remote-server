"""
处理 ego-browser 设备注册、凭据与撤销。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from agent_remote_server.ego_browser.relay import (
    EgoBrowserProofChallengeClaims,
)
from agent_remote_server.models import (
    EgoBrowserDevice,
    EgoBrowserDeviceCredential,
    EgoBrowserEnsureRequest,
    User,
)
from agent_remote_server.models.ego_browser import (
    MAX_EGO_BROWSER_GENERATION,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserDeviceRegisterRequest,
    EgoBrowserDeviceRevokeRequest,
    EgoBrowserProofChallengeRequest,
)
from agent_remote_server.security import create_opaque_token, decrypt_text, encrypt_text, hash_token
from agent_remote_server.services.ego_browser.base import _EgoBrowserServiceBase
from agent_remote_server.services.ego_browser.contracts import (
    EgoBrowserDeviceCredentialIssue,
    EgoBrowserProofChallengeIssue,
)
from agent_remote_server.services.ego_browser.helpers import (
    _as_utc,
    _canonical_capabilities,
    _decode_encryption_public_key,
    _decode_public_key,
    _encode_public_key,
    _safe_reason,
    _validate_digest,
)


def _idempotency_payload_value(payload: EgoBrowserDeviceRegisterRequest) -> dict[str, object]:
    """
    生成幂等指纹使用的 payload，并统一两个别名的省略默认值。

    :param payload (EgoBrowserDeviceRegisterRequest): 载荷
    :return dict[str, object]: idempotency 载荷值
    """

    value: dict[str, object] = payload.model_dump(
        mode="json", exclude={"proof_challenge", "proof_signature"}
    )
    # 旧载荷省略模式时按首次登记计算指纹，同时保持显式生命周期模式互不混淆。
    if "enrollment_mode" not in payload.model_fields_set:
        value["enrollment_mode"] = "initial"
    return value


class _EgoBrowserDeviceOperations(_EgoBrowserServiceBase):
    """
    实现独立 ego-browser 设备操作。
    """

    async def issue_proof_challenge(
        self,
        *,
        user: User,
        payload: EgoBrowserProofChallengeRequest,
        authenticated_device: EgoBrowserDevice | None,
    ) -> EgoBrowserProofChallengeIssue:
        """
        签发与认证主体和目标操作绑定的一次性 PoP challenge。

        :param user (User): 当前操作用户
        :param payload (EgoBrowserProofChallengeRequest): PoP challenge 签发请求
        :param authenticated_device (EgoBrowserDevice | None): 凭据设备；用户认证时为 None
        :return EgoBrowserProofChallengeIssue: 短期 challenge 及其过期时间
        """

        operation = payload.operation
        # 清理独立于两项准入门禁，确保关闭准入后仍能收敛已授权绑定。
        cleanup_operations = {
            "pause_binding",
            "stop_binding",
            "revoke_binding",
            "revoke_device",
        }
        enrollment_operations = {"register_device", "device_rotate", "confirm_allowlist"}
        execution_operations = {
            "claim_binding",
            "connect_binding",
            "renew_binding",
            "resume_binding",
            "issue_relay_ticket",
        }
        if operation in enrollment_operations:
            self._require_enrollment()
        elif operation in execution_operations:
            self._require_execution()
        elif operation not in cleanup_operations:
            self._error(
                "EGO_BROWSER_POP_CONTEXT_INVALID",
                "The proof challenge operation is invalid.",
                422,
            )
        if self._relay_store is None:
            self._error(
                "EGO_BROWSER_POP_UNAVAILABLE",
                "The proof-of-possession challenge store is unavailable.",
                503,
            )
        binding_operations = {
            "connect_binding",
            "renew_binding",
            "pause_binding",
            "resume_binding",
            "stop_binding",
            "revoke_binding",
            "issue_relay_ticket",
            "confirm_allowlist",
        }
        if operation in {"register_device", "device_rotate"}:
            if authenticated_device is not None or payload.binding_id is not None:
                self._error(
                    "EGO_BROWSER_POP_CONTEXT_INVALID",
                    "The proof challenge context is invalid.",
                    403,
                )
            existing = await self._repository.get_device(payload.ego_browser_device_id)
            if existing is not None and existing.user_id != user.id:
                self._error(
                    "EGO_BROWSER_DEVICE_CONFLICT",
                    "The ego-browser device is unavailable.",
                    409,
                )
            # 轮换挑战由下一代密钥签名，认证上下文必须绑定请求的新代次。
            expected_device_generation = (
                payload.generation
                if operation == "device_rotate" and existing is not None
                else existing.generation
                if existing is not None
                else payload.generation
            )
            if (
                payload.device_generation is not None
                and payload.device_generation != expected_device_generation
            ):
                self._error(
                    "EGO_BROWSER_POP_CONTEXT_INVALID",
                    "The proof challenge device generation is stale.",
                    409,
                )
        else:
            if (
                authenticated_device is None
                or authenticated_device.id != payload.ego_browser_device_id
                or authenticated_device.user_id != user.id
            ):
                self._error(
                    "EGO_BROWSER_POP_CONTEXT_INVALID",
                    "The proof challenge context is invalid.",
                    403,
                )
            if (
                payload.device_generation is not None
                and payload.device_generation != authenticated_device.generation
            ):
                self._error(
                    "EGO_BROWSER_POP_CONTEXT_INVALID",
                    "The proof challenge device generation is stale.",
                    409,
                )
            if operation == "claim_binding":
                if (
                    payload.binding_id is not None
                    or payload.generation != authenticated_device.generation
                ):
                    self._error(
                        "EGO_BROWSER_POP_CONTEXT_INVALID",
                        "The proof challenge context is invalid.",
                        409,
                    )
            elif operation in binding_operations:
                if payload.binding_id is None:
                    self._error(
                        "EGO_BROWSER_POP_CONTEXT_INVALID",
                        "The proof challenge context is invalid.",
                        422,
                    )
                binding = await self._repository.get_binding(payload.binding_id)
                if (
                    binding is None
                    or binding.user_id != user.id
                    or binding.ego_browser_device_id != authenticated_device.id
                    or binding.generation != payload.generation
                ):
                    self._error(
                        "EGO_BROWSER_POP_CONTEXT_INVALID",
                        "The proof challenge context is stale or unavailable.",
                        409,
                    )
            elif operation == "revoke_device" and (
                payload.binding_id is not None
                or payload.generation != authenticated_device.generation
            ):
                self._error(
                    "EGO_BROWSER_POP_CONTEXT_INVALID",
                    "The proof challenge context is stale or unavailable.",
                    409,
                )

        challenge = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        ttl = self._settings.ego_browser_pop_challenge_ttl_seconds
        expires_at = self._now() + timedelta(seconds=ttl)
        await self._relay_store.issue_proof_challenge(
            token_hash=hash_token(self._settings.secret_key, challenge),
            claims=EgoBrowserProofChallengeClaims(
                user_id=user.id,
                ego_browser_device_id=payload.ego_browser_device_id,
                operation=operation,
                generation=payload.generation,
                binding_id=payload.binding_id,
            ),
            ttl=ttl,
        )
        return EgoBrowserProofChallengeIssue(
            challenge=challenge,
            expires_at=expires_at,
            device_generation=(
                payload.device_generation
                or (authenticated_device.generation if authenticated_device is not None else None)
                or payload.generation
            ),
            operation_generation=payload.operation_generation or payload.generation,
        )

    async def register_device(
        self,
        *,
        user: User,
        payload: EgoBrowserDeviceRegisterRequest,
        commit: bool = True,
        proof_operation: str = "register_device",
        require_rotation: bool = False,
        strict_identity: bool = False,
    ) -> EgoBrowserDevice:
        """
        注册或轮换当前用户的独立 ego-browser 设备。

        :param user (User): 当前操作用户
        :param payload (EgoBrowserDeviceRegisterRequest): 独立设备注册或密钥轮换请求
        :param commit (bool): 是否在操作完成后提交数据库事务
        :param proof_operation (str): 所有权证明对应的操作名称
        :param require_rotation (bool): 是否强制执行一次代次轮换
        :param strict_identity (bool): 已有记录存在时是否只允许完全一致的 identity
        :return EgoBrowserDevice: 注册、轮换或撤销后的独立设备实体
        :raises AssertionError: 新设备加密密钥校验不变量被破坏
        """
        self._require_enrollment()
        if payload.generation > MAX_EGO_BROWSER_GENERATION:
            self._error(
                "EGO_BROWSER_GENERATION_INVALID",
                "The device generation is outside the supported range.",
                422,
            )
        _validate_digest(payload.learning_bundle_digest, self._error)
        _validate_digest(payload.allowlist_roots_digest, self._error)
        capabilities = self._validate_policy_capabilities(
            payload.capabilities,
            allowlist_roots_digest=payload.allowlist_roots_digest,
            learning_bundle_digest=payload.learning_bundle_digest,
        )
        self._validate_policy_digests(
            policy_digest=payload.policy_digest,
            capability_digest=payload.capability_digest,
            allowlist_revision=payload.allowlist_revision,
            allowlist_roots_digest=payload.allowlist_roots_digest,
            learning_bundle_digest=payload.learning_bundle_digest,
            capabilities=capabilities,
        )
        self._validate_profile(
            release_profile=payload.release_profile,
            credential_profile=payload.credential_profile,
            signer_certificate_sha256=payload.signer_certificate_sha256,
        )
        public_key = _decode_public_key(payload.public_key)
        encryption_public_key = _decode_encryption_public_key(payload.encryption_public_key)
        await self._validate_pop(
            public_key=public_key,
            user_id=user.id,
            device_id=payload.device_id,
            device_generation=payload.generation,
            operation_generation=payload.generation,
            release_profile=payload.release_profile,
            credential_profile=payload.credential_profile,
            operation=proof_operation,
            binding_id=None,
            payload=payload,
        )

        existing = await self._repository.get_device(payload.device_id, for_update=True)
        if existing is None and payload.enrollment_mode == "ensure":
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The retained ego-browser device is not enrolled on this Server.",
                404,
            )
        if require_rotation and existing is None:
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The ego-browser device was not found for rotation.",
                404,
            )
        if existing is None and encryption_public_key is None:
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_REQUIRED",
                "An independent ego-browser encryption key is required.",
                422,
            )
        if (
            existing is not None
            and existing.encryption_public_key is None
            and encryption_public_key is None
        ):
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_REQUIRED",
                "The device must be re-registered with an encryption key.",
                422,
            )
        revoked_generations: list[tuple[UUID, int]] = []
        if existing is not None:
            if existing.user_id != user.id or existing.status == "revoked":
                self._error(
                    "EGO_BROWSER_DEVICE_CONFLICT", "The ego-browser device is unavailable.", 409
                )
            self._validate_device_origin(existing, bind_legacy=False)
            if payload.generation < existing.generation:
                self._error(
                    "EGO_BROWSER_GENERATION_MISMATCH",
                    "The device generation cannot move backwards.",
                    409,
                )
            if require_rotation:
                live_bindings = await self._repository.list_live_for_device(
                    existing.id, for_update=True
                )
                if live_bindings:
                    self._error(
                        "EGO_BROWSER_BINDING_CONFLICT",
                        "Stop all live bindings before rotating the device identity.",
                        409,
                    )
                if payload.generation != existing.generation + 1:
                    self._error(
                        "EGO_BROWSER_GENERATION_MISMATCH",
                        "A device rotation must advance exactly one generation.",
                        409,
                    )
            if payload.allowlist_revision < existing.allowlist_revision or (
                payload.allowlist_revision == existing.allowlist_revision
                and payload.allowlist_roots_digest != existing.allowlist_roots_digest
            ):
                self._error(
                    "EGO_BROWSER_REVISION_CONFLICT",
                    "The allowlist revision cannot move backwards or change in place.",
                    409,
                )
            canonical_public_key = _encode_public_key(public_key)
            canonical_encryption_key = (
                _encode_public_key(encryption_public_key)
                if encryption_public_key is not None
                else existing.encryption_public_key
            )
            if existing.encryption_public_key is not None:
                _decode_encryption_public_key(existing.encryption_public_key)
            signing_key_changed = existing.public_key != canonical_public_key
            encryption_key_changed = existing.encryption_public_key != canonical_encryption_key
            if strict_identity:
                # ensure 只能刷新短期凭据，不能静默改变身份、发布、运行时或策略元数据。
                if payload.generation != existing.generation:
                    self._error(
                        "EGO_BROWSER_GENERATION_MISMATCH",
                        "The retained device generation does not match the Server identity.",
                        409,
                    )
                if encryption_public_key is None or signing_key_changed or encryption_key_changed:
                    self._error(
                        "EGO_BROWSER_DEVICE_CONFLICT",
                        "The ensure payload does not match the retained device identity.",
                        409,
                    )
                metadata_matches = (
                    existing.platform == payload.platform
                    and existing.release_profile == payload.release_profile
                    and existing.signer_certificate_sha256 == payload.signer_certificate_sha256
                    and existing.credential_profile == payload.credential_profile
                    and existing.bridge_protocol_version == payload.bridge_protocol_version
                    and existing.bridge_version == payload.bridge_version
                    and existing.local_ego_browser_runtime_version
                    == payload.local_ego_browser_runtime_version
                    and existing.ego_lite_runtime_version == payload.ego_lite_runtime_version
                    and existing.skill_version == payload.skill_version
                    and _canonical_capabilities(existing.capabilities) == capabilities
                    and existing.allowlist_revision == payload.allowlist_revision
                    and existing.allowlist_roots_digest == payload.allowlist_roots_digest
                    and existing.learning_bundle_digest == payload.learning_bundle_digest
                )
                if not metadata_matches:
                    self._error(
                        "EGO_BROWSER_DEVICE_CONFLICT",
                        "The ensure payload does not match the retained device metadata.",
                        409,
                    )
                self._bind_legacy_device_origin(existing)
                existing.status = "active"
                existing.revoked_at = None
                existing.last_seen_at = self._now()
                if commit:
                    await self._session.commit()
                return existing

            if require_rotation and not (signing_key_changed or encryption_key_changed):
                self._error(
                    "EGO_BROWSER_ROTATION_KEY_UNCHANGED",
                    "A device rotation must submit a new signing or encryption key.",
                    409,
                )
            policy_changed = (
                existing.allowlist_revision != payload.allowlist_revision
                or existing.allowlist_roots_digest != payload.allowlist_roots_digest
                or existing.learning_bundle_digest != payload.learning_bundle_digest
                or _canonical_capabilities(existing.capabilities) != capabilities
            )
            if signing_key_changed or encryption_key_changed:
                if payload.generation <= existing.generation:
                    self._error(
                        "EGO_BROWSER_GENERATION_MISMATCH",
                        "A key rotation requires a newer device generation.",
                        409,
                    )
                existing.public_key = canonical_public_key
                existing.encryption_public_key = canonical_encryption_key
                existing.generation = payload.generation
                revoked_generations.extend(
                    await self._invalidate_bindings(
                        await self._repository.list_live_for_device(
                            existing.id,
                            for_update=True,
                        ),
                        terminal_status="revoked",
                        reason="device_key_rotated",
                        actor_user_id=user.id,
                    )
                )
            elif policy_changed:
                for binding in await self._repository.list_live_for_device(
                    existing.id,
                    for_update=True,
                ):
                    old_generation = binding.generation
                    await self._terminalize_generation_requests(
                        binding=binding,
                        generation=old_generation,
                        reason="local_policy_changed",
                        actor_user_id=user.id,
                    )
                    self._advance_generation(binding)
                    binding.status = "paused"
                    binding.lease_until = None
                    binding.lease_health = "expired"
                    binding.lease_grace_until = None
                    binding.stop_reason = "local_policy_changed"
                    await self._enqueue_revocation(
                        binding,
                        old_generation,
                        "local_policy_changed",
                    )
                    await self._audit(
                        user.id,
                        "ego_browser_binding.paused",
                        str(binding.id),
                        self._binding_details(binding),
                    )
                    revoked_generations.append((binding.id, old_generation))
            self._bind_legacy_device_origin(existing)
            self._update_device_metadata(existing, payload, canonical_encryption_key)
            existing.status = "active"
            existing.revoked_at = None
            existing.last_seen_at = self._now()
            if commit:
                await self._session.commit()
                for binding_id, generation in revoked_generations:
                    await self._publish_revocation(binding_id, generation)
            return existing

        if encryption_public_key is None:
            raise AssertionError("encryption key was not validated")
        device = EgoBrowserDevice(
            id=payload.device_id,
            user_id=user.id,
            public_key=_encode_public_key(public_key),
            encryption_public_key=_encode_public_key(encryption_public_key),
            generation=payload.generation,
            status="active",
            platform=payload.platform,
            release_profile=payload.release_profile,
            signer_certificate_sha256=payload.signer_certificate_sha256,
            credential_profile=payload.credential_profile,
            bridge_protocol_version=payload.bridge_protocol_version,
            bridge_version=payload.bridge_version,
            local_ego_browser_runtime_version=payload.local_ego_browser_runtime_version,
            ego_lite_runtime_version=payload.ego_lite_runtime_version,
            skill_version=payload.skill_version,
            capabilities=capabilities,
            allowlist_revision=payload.allowlist_revision,
            allowlist_roots_digest=payload.allowlist_roots_digest,
            learning_bundle_digest=payload.learning_bundle_digest,
            server_origin=self._settings.public_origin,
            last_seen_at=self._now(),
        )
        try:
            await self._repository.add_device(device)
            await self._audit(
                user.id,
                "ego_browser_device.registered",
                str(device.id),
                {"generation": device.generation, "release_profile": device.release_profile},
            )
            if commit:
                await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            self._error(
                "EGO_BROWSER_DEVICE_CONFLICT", "The ego-browser device is already registered.", 409
            )
        if commit:
            await self.publish_pending_revocations()
        return device

    async def ensure_device(
        self,
        *,
        user: User,
        payload: EgoBrowserDeviceRegisterRequest,
        idempotency_key: str | None = None,
        commit: bool = True,
        logical_operation: str = "device.ensure",
        proof_operation: str = "register_device",
        require_rotation: bool = False,
        strict_identity: bool | None = None,
    ) -> tuple[EgoBrowserDevice, EgoBrowserDeviceCredentialIssue]:
        """
        幂等登记设备并返回可恢复的短期凭据。

        :param user (User): 当前操作用户
        :param payload (EgoBrowserDeviceRegisterRequest): 独立设备登记请求
        :param idempotency_key (str | None): 请求幂等键
        :param commit (bool): 是否在操作完成后提交数据库事务
        :param logical_operation (str): 幂等记录使用的逻辑操作名称
        :param proof_operation (str): 所有权证明对应的操作名称
        :param require_rotation (bool): 是否要求设备密钥轮换
        :param strict_identity (bool | None): 是否拒绝身份或元数据漂移；默认由登记模式决定
        :return tuple[EgoBrowserDevice, EgoBrowserDeviceCredentialIssue]: 设备与短期凭据
        """

        self._require_enrollment()
        effective_strict_identity = (
            payload.enrollment_mode == "ensure" if strict_identity is None else strict_identity
        )
        if effective_strict_identity and not idempotency_key:
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_INVALID",
                "An Idempotency-Key is required for ensure.",
                422,
            )
        key = idempotency_key or self._legacy_idempotency_key(payload)
        if (
            len(key) < 22
            or len(key) > 256
            or any(not character.isprintable() or character in {'"', "\\"} for character in key)
        ):
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_INVALID",
                "The ensure idempotency key is invalid.",
                422,
            )
        key_hash = hash_token(self._settings.secret_key, key)
        fingerprint = self._ensure_fingerprint(payload)

        # 锁定父用户行可串行化“子设备不存在”的插入竞态，单锁子行无法覆盖该场景。
        await self._repository.get_user_for_update(user.id)
        request = await self._repository.get_ensure_request(
            user_id=user.id,
            logical_operation=logical_operation,
            idempotency_key_hash=key_hash,
            for_update=True,
        )
        replay_device: EgoBrowserDevice | None = None
        if request is not None:
            if request.request_fingerprint != fingerprint:
                self._error(
                    "EGO_BROWSER_IDEMPOTENCY_CONFLICT",
                    "The idempotency key was reused with a different request.",
                    409,
                )
            # 缓存仅抑制重复副作用，重放仍须通过新签发的一次性 Device-PoP 挑战认证。
            replay_device = await self._repository.get_device(
                request.ego_browser_device_id, for_update=True
            )
            if replay_device is not None:
                self._validate_device_origin(replay_device, bind_legacy=False)
                await self._validate_device_pop(
                    device=replay_device,
                    operation_generation=replay_device.generation,
                    operation=proof_operation,
                    binding_id=None,
                    payload=payload,
                )
            replay = await self._replay_ensure_request(request)
            if replay is not None:
                self._bind_legacy_device_origin(replay[0])
                if commit:
                    # 返回缓存凭据前持久化校验阶段完成的一次性旧来源迁移。
                    await self._session.commit()
                return replay

            # 丢失的轮换响应只能在身份和发布字段完全匹配时恢复，禁止再次推进代次。
            if (
                require_rotation
                and replay_device is not None
                and self._rotation_recovery_matches(replay_device, payload)
                and not await self._repository.list_live_for_device(
                    replay_device.id, for_update=True
                )
            ):
                credential = await self.issue_device_credential(
                    user=user,
                    device_id=replay_device.id,
                    commit=False,
                )
                request.credential_id = credential.credential.id
                request.encrypted_access_token = encrypt_text(
                    self._settings.secret_key, credential.raw_token
                )
                request.credential_revision = credential.credential.revision
                request.credential_expires_at = credential.credential.expires_at
                request.result_expires_at = self._now() + timedelta(
                    seconds=self._settings.ego_browser_ensure_result_retention_seconds
                )
                request.consumed_at = self._now()
                await self._audit(
                    user.id,
                    "ego_browser_device.rotate_recovered",
                    str(replay_device.id),
                    {
                        "generation": replay_device.generation,
                        "idempotency_key_hash": key_hash,
                    },
                )
                if commit:
                    await self._session.commit()
                    await self.publish_pending_revocations()
                return replay_device, credential

        device = await self.register_device(
            user=user,
            payload=payload,
            commit=False,
            proof_operation=proof_operation,
            require_rotation=require_rotation,
            strict_identity=effective_strict_identity and not require_rotation,
        )
        credential = await self.issue_device_credential(
            user=user,
            device_id=device.id,
            commit=False,
        )
        if request is None:
            request = EgoBrowserEnsureRequest(
                user_id=user.id,
                ego_browser_device_id=device.id,
                logical_operation=logical_operation,
                idempotency_key_hash=key_hash,
                request_fingerprint=fingerprint,
            )
            await self._repository.add_ensure_request(request)
        else:
            request.ego_browser_device_id = device.id
        request.credential_id = credential.credential.id
        request.encrypted_access_token = encrypt_text(
            self._settings.secret_key, credential.raw_token
        )
        request.credential_revision = credential.credential.revision
        request.credential_expires_at = credential.credential.expires_at
        request.result_expires_at = self._now() + timedelta(
            seconds=self._settings.ego_browser_ensure_result_retention_seconds
        )
        request.consumed_at = self._now()
        await self._audit(
            user.id,
            "ego_browser_device.ensure_completed",
            str(device.id),
            {"generation": device.generation, "idempotency_key_hash": key_hash},
        )
        if commit:
            await self._session.commit()
            await self.publish_pending_revocations()
        return device, credential

    @staticmethod
    def _rotation_recovery_matches(
        device: EgoBrowserDevice,
        payload: EgoBrowserDeviceRegisterRequest,
    ) -> bool:
        """
        判断当前设备是否仍是该 rotation 幂等请求提交的目标状态。

        :param device (EgoBrowserDevice): 设备
        :param payload (EgoBrowserDeviceRegisterRequest): 载荷
        :return bool: 是否满足校验条件
        """

        public_key = _decode_public_key(payload.public_key)
        encryption_key = _decode_encryption_public_key(payload.encryption_public_key)
        canonical_public_key = _encode_public_key(public_key)
        canonical_encryption_key = (
            _encode_public_key(encryption_key)
            if encryption_key is not None
            else device.encryption_public_key
        )
        return (
            device.id == payload.device_id
            and device.status == "active"
            and device.generation == payload.generation
            and device.public_key == canonical_public_key
            and device.encryption_public_key == canonical_encryption_key
            and device.release_profile == payload.release_profile
            and device.signer_certificate_sha256 == payload.signer_certificate_sha256
            and device.credential_profile == payload.credential_profile
            and device.bridge_protocol_version == payload.bridge_protocol_version
            and device.bridge_version == payload.bridge_version
            and device.local_ego_browser_runtime_version
            == payload.local_ego_browser_runtime_version
            and device.ego_lite_runtime_version == payload.ego_lite_runtime_version
            and device.skill_version == payload.skill_version
            and _canonical_capabilities(device.capabilities)
            == _canonical_capabilities(payload.capabilities)
            and device.allowlist_revision == payload.allowlist_revision
            and device.allowlist_roots_digest == payload.allowlist_roots_digest
            and device.learning_bundle_digest == payload.learning_bundle_digest
        )

    async def rotate_device(
        self,
        *,
        user: User,
        payload: EgoBrowserDeviceRegisterRequest,
        idempotency_key: str,
        commit: bool = True,
    ) -> tuple[EgoBrowserDevice, EgoBrowserDeviceCredentialIssue]:
        """
        在独立幂等命名空间中轮换设备密钥。

        :param user (User): 当前操作用户
        :param payload (EgoBrowserDeviceRegisterRequest): 新设备身份与发布元数据
        :param idempotency_key (str): 轮换请求的幂等键
        :param commit (bool): 是否在操作完成后提交数据库事务
        :return tuple[EgoBrowserDevice, EgoBrowserDeviceCredentialIssue]: 轮换后的设备与凭据
        """

        if not idempotency_key:
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_INVALID",
                "A rotation idempotency key is required.",
                422,
            )
        return await self.ensure_device(
            user=user,
            payload=payload,
            idempotency_key=idempotency_key,
            commit=commit,
            logical_operation="device.rotate",
            proof_operation="device_rotate",
            require_rotation=True,
        )

    async def _replay_ensure_request(
        self, request: EgoBrowserEnsureRequest
    ) -> tuple[EgoBrowserDevice, EgoBrowserDeviceCredentialIssue] | None:
        """
        在同一幂等 key 的短期恢复窗口内重放成功响应。

        :param request (EgoBrowserEnsureRequest): HTTP 请求
        :return tuple[EgoBrowserDevice, EgoBrowserDeviceCredentialIssue] | None: replay 确保请求
        """

        now = self._now()
        # 交换材料过期后仍保留幂等指纹；结果过期只触发同一身份的新凭据签发。
        if request.result_expires_at is not None and _as_utc(request.result_expires_at) <= now:
            return None

        # 已有幂等记录却缺失或冲突缓存时必须拒绝，避免存储故障签发第二份凭据。
        if (
            request.credential_id is None
            or request.encrypted_access_token is None
            or request.credential_revision is None
            or request.credential_expires_at is None
            or request.consumed_at is None
        ):
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                "The cached ensure result is incomplete and cannot be recovered.",
                503,
            )
        credential = await self._repository.get_device_credential(
            request.credential_id, for_update=True
        )
        device = await self._repository.get_device(request.ego_browser_device_id, for_update=True)
        if (
            credential is None
            or device is None
            or request.user_id != device.user_id
            or credential.user_id != request.user_id
            or credential.ego_browser_device_id != device.id
            or device.status != "active"
            or credential.generation != device.generation
            or credential.credential_profile != device.credential_profile
            or credential.revision != request.credential_revision
        ):
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                "The cached ensure result does not match the enrolled device.",
                503,
            )
        expires_at = _as_utc(credential.expires_at)
        cached_expires_at = _as_utc(request.credential_expires_at)
        if expires_at != cached_expires_at:
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                "The cached ensure credential expiry is inconsistent.",
                503,
            )
        try:
            raw_token = decrypt_text(self._settings.secret_key, request.encrypted_access_token)
        except Exception:
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                "The cached ensure credential cannot be decrypted.",
                503,
            )
        if hash_token(self._settings.secret_key, raw_token) != credential.token_hash:
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                "The cached ensure credential failed integrity validation.",
                503,
            )
        # 过期是正常状态转换；完整校验缓存后可为同一身份签发新凭据。
        if expires_at <= now:
            if credential.status not in {"active", "expired"}:
                self._error(
                    "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                    "The cached ensure credential has an invalid status.",
                    503,
                )
            return None
        if credential.status != "active":
            self._error(
                "EGO_BROWSER_IDEMPOTENCY_RESULT_INVALID",
                "The cached ensure credential has an invalid status.",
                503,
            )
        return device, EgoBrowserDeviceCredentialIssue(
            credential=credential,
            raw_token=raw_token,
            expires_in=max(1, int((expires_at - now).total_seconds())),
        )

    @staticmethod
    def _ensure_fingerprint(payload: EgoBrowserDeviceRegisterRequest) -> str:
        """
        确保指纹。

        :param payload (EgoBrowserDeviceRegisterRequest): 载荷
        :return str: 指纹
        """
        value = _idempotency_payload_value(payload)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _legacy_idempotency_key(payload: EgoBrowserDeviceRegisterRequest) -> str:
        """
        为没有 header 的旧 register 客户端生成稳定的兼容 key。

        :param payload (EgoBrowserDeviceRegisterRequest): 载荷
        :return str: 兼容版本幂等键
        """

        value = _idempotency_payload_value(payload)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "legacy-" + hashlib.sha256(encoded.encode()).hexdigest()

    async def list_devices(self, *, user: User, all_users: bool = False) -> list[EgoBrowserDevice]:
        """
        列出当前用户的独立 ego-browser 设备。

        :param user (User): 当前操作用户
        :param all_users (bool): 是否包含其他用户拥有的记录
        :return list[EgoBrowserDevice]: 符合权限条件的独立设备列表
        """

        self._require_enrollment()
        if all_users:
            if user.role != "admin":
                self._error("COMMON_FORBIDDEN", "Administrator role is required.", 403)
            return list(await self._repository.list_all_devices())
        return list(await self._repository.list_devices(user.id))

    async def expire_ensure_results(self, *, limit: int | None = None, commit: bool = True) -> int:
        """
        清理过期交换材料，同时保留幂等指纹和设备关联以检测冲突。

        :param limit (int | None): 单批清理上限
        :param commit (bool): 是否提交清理事务
        :return int: 清理的记录数
        """

        rows = await self._repository.list_expired_ensure_requests(
            self._now(),
            limit=limit or self._settings.ego_browser_cleanup_batch_size,
            for_update=True,
        )
        for row in rows:
            row.encrypted_access_token = None
            row.credential_id = None
            row.credential_revision = None
            row.credential_expires_at = None
        if rows and commit:
            await self._session.commit()
        return len(rows)

    async def issue_device_credential(
        self, *, user: User, device_id: UUID, commit: bool = True
    ) -> EgoBrowserDeviceCredentialIssue:
        """
        为已注册设备轮换独立短期凭据，并只返回一次原始 token。

        :param user (User): 当前操作用户
        :param device_id (UUID): 独立 ego-browser 设备 ID
        :param commit (bool): 是否在操作完成后提交数据库事务
        :return EgoBrowserDeviceCredentialIssue: 设备凭据记录及仅返回一次的原始 token
        """

        self._require_enrollment()
        device = await self._repository.get_device(device_id, for_update=True)
        if device is None or device.user_id != user.id or device.status != "active":
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The ego-browser device was not found.",
                404,
            )
        self._validate_device_origin(device, bind_legacy=False)
        credentials = list(
            await self._repository.list_device_credentials(device.id, for_update=True)
        )
        now = self._now()
        next_revision = 1
        for credential in credentials:
            next_revision = max(next_revision, credential.revision + 1)
            if credential.status == "active":
                credential.status = "revoked"
                credential.revoked_at = now
        raw_token = create_opaque_token("egbc")
        expires_in = self._settings.ego_browser_device_credential_ttl_seconds
        credential = EgoBrowserDeviceCredential(
            user_id=user.id,
            ego_browser_device_id=device.id,
            token_hash=hash_token(self._settings.secret_key, raw_token),
            credential_profile=device.credential_profile,
            generation=device.generation,
            revision=next_revision,
            status="active",
            expires_at=now + timedelta(seconds=expires_in),
        )
        self._bind_legacy_device_origin(device)
        await self._repository.add_device_credential(credential)
        await self._audit(
            user.id,
            "ego_browser_device.credential_issued",
            str(device.id),
            {
                "credential_id": str(credential.id),
                "generation": credential.generation,
                "revision": credential.revision,
            },
        )
        if commit:
            await self._session.commit()
        return EgoBrowserDeviceCredentialIssue(
            credential=credential,
            raw_token=raw_token,
            expires_in=expires_in,
        )

    async def revoke_device(
        self,
        *,
        user: User,
        device_id: UUID,
        payload: EgoBrowserDeviceRevokeRequest,
        authenticated_device_id: UUID | None = None,
    ) -> EgoBrowserDevice:
        """
        原子撤销独立设备、全部 live binding 和全部 active 凭据。

        :param user (User): 当前操作用户
        :param device_id (UUID): 独立 ego-browser 设备 ID
        :param payload (EgoBrowserDeviceRevokeRequest): 独立设备永久撤销请求
        :param authenticated_device_id (UUID | None): 凭据设备 ID；用户认证时为 None
        :return EgoBrowserDevice: 注册、轮换或撤销后的独立设备实体
        """

        device = await self._repository.get_device(device_id, for_update=True)
        if device is None or (device.user_id != user.id and user.role != "admin"):
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The ego-browser device was not found.",
                404,
            )
        if device.status == "revoked":
            return device
        # 用户或管理员可跨来源收敛旧身份；设备凭据撤销仍严格限定其规范来源。
        if authenticated_device_id is not None:
            self._validate_device_origin(device, bind_legacy=False)
        if device.generation != payload.generation:
            self._generation_error()
        if authenticated_device_id is not None:
            if authenticated_device_id != device.id:
                self._error(
                    "EGO_BROWSER_DEVICE_NOT_FOUND",
                    "The ego-browser device was not found.",
                    404,
                )
            await self._validate_device_pop(
                device=device,
                operation_generation=device.generation,
                operation="revoke_device",
                binding_id=None,
                payload=payload,
            )
            self._bind_legacy_device_origin(device)

        now = self._now()
        revoked_generations = await self._invalidate_bindings(
            await self._repository.list_live_for_device(device.id, for_update=True),
            terminal_status="revoked",
            reason=payload.reason,
            actor_user_id=user.id,
            now=now,
        )
        revoked_credentials = 0
        for credential in await self._repository.list_device_credentials(
            device.id, for_update=True
        ):
            if credential.status != "active":
                continue
            credential.status = "revoked"
            credential.revoked_at = now
            revoked_credentials += 1
        device.status = "revoked"
        device.revoked_at = now
        await self._audit(
            user.id,
            "ego_browser_device.revoked",
            str(device.id),
            {
                "generation": device.generation,
                "revoked_bindings": len(revoked_generations),
                "revoked_credentials": revoked_credentials,
                "reason": _safe_reason(payload.reason),
            },
        )
        await self._session.commit()
        for binding_id, generation in revoked_generations:
            await self._publish_revocation(binding_id, generation)
        return device

    async def delete_device(self, *, user: User, device_id: UUID) -> None:
        """
        删除已撤销且无绑定历史、撤销事件已发布的独立设备。

        :param user (User): 当前操作用户
        :param device_id (UUID): 独立 ego-browser 设备 ID
        :raises ApiError: 设备不存在、尚未撤销或仍保留控制历史
        """

        device = await self._repository.get_device(device_id, for_update=True)
        if device is None or (device.user_id != user.id and user.role != "admin"):
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The ego-browser device was not found.",
                404,
            )
        if device.status != "revoked":
            self._error(
                "EGO_BROWSER_DEVICE_DELETE_REQUIRES_REVOKED",
                "Revoke the ego-browser device before deleting it.",
                409,
            )
        if await self._repository.has_any_for_device(device.id):
            self._error(
                "EGO_BROWSER_DEVICE_DELETE_BINDING_HISTORY",
                "Delete all ego-browser binding history before deleting the device.",
                409,
            )
        await self._audit(
            user.id,
            "ego_browser_device.deleted",
            str(device.id),
            {
                "device_user_id": str(device.user_id),
                "generation": device.generation,
                "status": device.status,
            },
        )
        await self._repository.delete_device(device)
        await self._session.commit()
