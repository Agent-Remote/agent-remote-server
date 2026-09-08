"""处理 ego-browser 绑定查询、候选与认领。"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from agent_remote_server.models import (
    EgoBrowserBinding,
    Node,
    User,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserBindingClaimRequest,
)
from agent_remote_server.services.ego_browser.contracts import (
    SAFE_LABEL,
    EgoBrowserClaimResult,
)
from agent_remote_server.services.ego_browser.devices import _EgoBrowserDeviceOperations
from agent_remote_server.services.ego_browser.helpers import (
    _canonical_capabilities,
    _decode_encryption_public_key,
    _validate_digest,
)


class _EgoBrowserBindingOperations(_EgoBrowserDeviceOperations):
    """实现绑定发现和显式认领操作。"""

    async def list_bindings(
        self, *, user: User, all_users: bool = False
    ) -> list[EgoBrowserBinding]:
        """
        列出当前用户的 ego-browser 绑定。

        :param user (User): 当前操作用户
        :param all_users (bool): 是否包含其他用户拥有的记录

        :return list[EgoBrowserBinding]: 符合权限与状态条件的绑定列表
        """

        await self.expire_due()
        if all_users:
            if user.role != "admin":
                self._error("COMMON_FORBIDDEN", "Administrator role is required.", 403)
            return list(await self._repository.list_all_bindings())
        return list(await self._repository.list_bindings(user.id))

    async def list_node_bindings(self, *, node: Node) -> list[EgoBrowserBinding]:
        """
        列出当前 Node 承载的 live binding 元数据。

        :param node (Node): 当前操作对应的节点

        :return list[EgoBrowserBinding]: 符合权限与状态条件的 binding 列表
        """

        self._require_enabled()
        # 清理在本次读取前已经过期的 generation，避免 broker 获得可执行的陈旧租约。
        await self.expire_due()
        return list(await self._repository.list_live_for_node(node.id))

    async def get_binding(self, *, user: User, binding_id: UUID) -> EgoBrowserBinding:
        """
        读取当前用户拥有的单个 ego-browser binding。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """

        binding = await self._repository.get_binding(binding_id)
        if binding is None or (binding.user_id != user.id and user.role != "admin"):
            self._error(
                "EGO_BROWSER_BINDING_NOT_FOUND",
                "The browser binding was not found.",
                404,
            )
        return binding

    async def list_candidates(self, *, user: User) -> list[dict[str, object]]:
        """
        列出可供明确选择的远端 Claude session。

        :param user (User): 当前操作用户

        :return list[dict[str, object]]: 可供用户明确选择的远端 session 候选列表
        """
        rows = await self._repository.list_candidates(user.id)
        candidates: list[dict[str, object]] = []
        for tool_session, node, binding, workspace in rows:
            device = None
            if binding is not None:
                device = await self._repository.get_device(binding.ego_browser_device_id)
            candidates.append(
                {
                    "tool_session_id": tool_session.id,
                    "tool_type": "claude",
                    "tool_account_id": tool_session.tool_account_id,
                    "workspace_id": tool_session.workspace_id,
                    "project_key": tool_session.project_key,
                    "display_name": workspace.display_name,
                    "status": tool_session.status,
                    "node_id": node.id,
                    "runtime_backend": tool_session.runtime_backend,
                    "current_ego_browser_device_id": device.id if device else None,
                    "current_ego_browser_device_name": None,
                    "binding_id": binding.id if binding else None,
                    "controllable": (
                        binding is None
                        and self._node_supports_backend(node, tool_session.runtime_backend)
                        and node.status in {"healthy", "degraded"}
                    ),
                }
            )
        return candidates

    async def claim(
        self,
        *,
        user: User,
        payload: EgoBrowserBindingClaimRequest,
        device_id: UUID | None = None,
    ) -> EgoBrowserClaimResult:
        """
        原子创建一个经用户确认的 ego-browser binding。

        :param user (User): 当前操作用户
        :param payload (EgoBrowserBindingClaimRequest): binding 显式认领请求
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserClaimResult: 新 binding 及被替换 generation 的认领结果
        """
        self._require_enabled()
        _validate_digest(payload.learning_bundle_digest, self._error)
        if (
            payload.authorization_mode != "ego_browser_script_full_trust"
            or payload.authorization_policy_version != 1
        ):
            self._error(
                "EGO_BROWSER_AUTHORIZATION_UNSUPPORTED",
                "Unsupported ego-browser authorization policy.",
                409,
            )
        device = await self._repository.get_device(payload.ego_browser_device_id, for_update=True)
        if device is None or device.user_id != user.id or device.status != "active":
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND", "The ego-browser device was not found.", 404
            )
        self._validate_profile(
            release_profile=device.release_profile,
            credential_profile=device.credential_profile,
            signer_certificate_sha256=device.signer_certificate_sha256,
        )
        if device_id is None:
            self._error(
                "EGO_BROWSER_CREDENTIAL_REQUIRED",
                "An independent ego-browser device credential is required.",
                403,
            )
        if device.id != device_id:
            self._error(
                "EGO_BROWSER_CREDENTIAL_DEVICE_MISMATCH",
                "The device credential does not match the selected device.",
                403,
            )
        if device.encryption_public_key is None:
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_REQUIRED",
                "The selected device has no registered encryption key.",
                409,
            )
        registered_encryption_key = _decode_encryption_public_key(device.encryption_public_key)
        requested_encryption_key = _decode_encryption_public_key(payload.encryption_public_key)
        if (
            requested_encryption_key is not None
            and requested_encryption_key != registered_encryption_key
        ):
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_MISMATCH",
                "The selected device encryption key does not match registration.",
                409,
            )
        if (
            device.release_profile != payload.release_profile
            or device.credential_profile != payload.credential_profile
        ):
            self._error(
                "EGO_BROWSER_PROFILE_MISMATCH", "The device release profile does not match.", 409
            )
        registered_capabilities = _canonical_capabilities(device.capabilities)
        capabilities = _canonical_capabilities(payload.device_capabilities)
        if capabilities != registered_capabilities:
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The selected device capability set does not match registration.",
                409,
            )
        capabilities = self._validate_policy_capabilities(
            registered_capabilities,
            allowlist_roots_digest=device.allowlist_roots_digest,
            learning_bundle_digest=device.learning_bundle_digest,
        )
        if (
            device.allowlist_revision != payload.allowlist_revision
            or device.learning_bundle_digest != payload.learning_bundle_digest
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The local capability revision does not match.",
                409,
            )
        await self._validate_device_pop(
            device=device,
            operation_generation=device.generation,
            operation="claim_binding",
            binding_id=None,
            payload=payload,
        )
        if payload.task_space_label is not None and not SAFE_LABEL.fullmatch(
            payload.task_space_label
        ):
            self._error("EGO_BROWSER_INVALID_TASK_SPACE", "The task-space label is invalid.", 422)
        tool_session = await self._repository.get_session(payload.tool_session_id, for_update=True)
        if tool_session is None or tool_session.user_id != user.id:
            self._error("COMMON_NOT_FOUND", "Tool session was not found.", 404)
        if tool_session.tool_type != "claude" or tool_session.status not in {
            "running",
            "active",
            "detached",
        }:
            self._error(
                "EGO_BROWSER_SESSION_UNAVAILABLE",
                "The selected tool session is not available.",
                409,
            )
        task_space_label = f"agent-remote:{tool_session.id}"
        if payload.task_space_label is not None and payload.task_space_label != task_space_label:
            self._error(
                "EGO_BROWSER_INVALID_TASK_SPACE",
                "The task-space label does not match the selected tool session.",
                422,
            )
        node = await self._repository.get_node(tool_session.node_id)
        if node is None or node.status not in {"healthy", "degraded"}:
            self._error("EGO_BROWSER_NODE_UNAVAILABLE", "The assigned node is not available.", 409)
        self._validate_node_capability(node, runtime_backend=tool_session.runtime_backend)
        await self._repository.acquire_user_lock(user.id)
        live_device = list(await self._repository.list_live_for_device(device.id, for_update=True))
        live_session = list(
            await self._repository.list_live_for_session(tool_session.id, for_update=True)
        )
        if live_device or live_session:
            self._error(
                "EGO_BROWSER_BINDING_CONFLICT",
                "The selected device or tool session already has a live browser binding.",
                409,
            )
        now = self._now()
        binding = EgoBrowserBinding(
            user_id=user.id,
            ego_browser_device_id=device.id,
            tool_session_id=tool_session.id,
            tool_session_reference_id=tool_session.id,
            node_id=node.id,
            status="pending_device",
            control_channel="ego_browser_bridge",
            relay_binding_kind="ego_browser",
            authorization_mode="ego_browser_script_full_trust",
            authorization_policy_version=1,
            authorized_at=now,
            release_profile=device.release_profile,
            signer_certificate_sha256=device.signer_certificate_sha256,
            credential_profile=device.credential_profile,
            remote_platform="linux",
            local_platform="macos",
            local_runtime_version=device.local_ego_browser_runtime_version,
            ego_lite_runtime_version=device.ego_lite_runtime_version,
            skill_version=device.skill_version,
            bridge_protocol_version=device.bridge_protocol_version,
            task_space_label=task_space_label,
            allowlist_revision=device.allowlist_revision,
            allowlist_roots_digest=device.allowlist_roots_digest,
            learning_bundle_digest=device.learning_bundle_digest,
            concurrency_mode=payload.concurrency_mode,
            max_parallel_requests=self._settings.ego_browser_max_parallel_requests,
            capabilities=list(capabilities),
            lease_until=now + timedelta(seconds=self._settings.ego_browser_lease_seconds),
            lease_health="healthy",
            lease_renew_interval_seconds=self._settings.ego_browser_lease_renew_interval_seconds,
            lease_renew_failure_grace_seconds=self._settings.ego_browser_lease_renew_failure_grace_seconds,
            absolute_ttl_until=now
            + timedelta(seconds=self._settings.ego_browser_absolute_ttl_seconds),
            generation=1,
        )
        try:
            await self._repository.add_binding(binding)
            await self._audit(
                user.id,
                "ego_browser_binding.created",
                str(binding.id),
                self._binding_details(binding),
            )
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            self._error(
                "EGO_BROWSER_BINDING_CONFLICT", "The browser binding is already in use.", 409
            )
        return EgoBrowserClaimResult(binding=binding)
