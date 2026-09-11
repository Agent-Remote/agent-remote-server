"""处理 ego-browser 设备注册、凭据与撤销。"""

from __future__ import annotations

import base64
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
from agent_remote_server.security import create_opaque_token, hash_token
from agent_remote_server.services.ego_browser.base import _EgoBrowserServiceBase
from agent_remote_server.services.ego_browser.contracts import (
    EgoBrowserDeviceCredentialIssue,
    EgoBrowserProofChallengeIssue,
)
from agent_remote_server.services.ego_browser.helpers import (
    _canonical_capabilities,
    _decode_encryption_public_key,
    _decode_public_key,
    _encode_public_key,
    _safe_reason,
    _validate_digest,
)


class _EgoBrowserDeviceOperations(_EgoBrowserServiceBase):
    """实现独立 ego-browser 设备操作。"""

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

        self._require_enabled()
        if self._relay_store is None:
            self._error(
                "EGO_BROWSER_POP_UNAVAILABLE",
                "The proof-of-possession challenge store is unavailable.",
                503,
            )
        operation = payload.operation
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
        if operation == "register_device":
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
        return EgoBrowserProofChallengeIssue(challenge=challenge, expires_at=expires_at)

    async def register_device(
        self,
        *,
        user: User,
        payload: EgoBrowserDeviceRegisterRequest,
        commit: bool = True,
    ) -> EgoBrowserDevice:
        """
        注册或轮换当前用户的独立 ego-browser 设备。

        :param user (User): 当前操作用户
        :param payload (EgoBrowserDeviceRegisterRequest): 独立设备注册或密钥轮换请求
        :param commit (bool): 是否在操作完成后提交数据库事务

        :return EgoBrowserDevice: 注册、轮换或撤销后的独立设备实体

        :raises AssertionError: 新设备加密密钥校验不变量被破坏
        """
        self._require_enabled()
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
            operation="register_device",
            binding_id=None,
            payload=payload,
        )

        existing = await self._repository.get_device(payload.device_id, for_update=True)
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
            if payload.generation < existing.generation:
                self._error(
                    "EGO_BROWSER_GENERATION_MISMATCH",
                    "The device generation cannot move backwards.",
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
            # 新设备分支已拒绝空密钥；显式断言同时固定静态类型和未来修改的不变量。
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

    async def list_devices(self, *, user: User, all_users: bool = False) -> list[EgoBrowserDevice]:
        """
        列出当前用户的独立 ego-browser 设备。

        :param user (User): 当前操作用户
        :param all_users (bool): 是否包含其他用户拥有的记录

        :return list[EgoBrowserDevice]: 符合权限条件的独立设备列表
        """

        if all_users:
            if user.role != "admin":
                self._error("COMMON_FORBIDDEN", "Administrator role is required.", 403)
            return list(await self._repository.list_all_devices())
        return list(await self._repository.list_devices(user.id))

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

        self._require_enabled()
        device = await self._repository.get_device(device_id, for_update=True)
        if device is None or device.user_id != user.id or device.status != "active":
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The ego-browser device was not found.",
                404,
            )
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
        删除已撤销且没有 binding 历史的独立设备。

        物理删除不会替代撤销流程；调用方必须先撤销设备并等待所有撤销事件发布。

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
