"""处理 ego-browser 文件 allowlist 元数据。"""

from __future__ import annotations

from uuid import UUID

from agent_remote_server.models import (
    EgoBrowserBinding,
    User,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserAllowlistConfirmRequest,
)
from agent_remote_server.services.ego_browser.admission import _EgoBrowserAdmissionOperations
from agent_remote_server.services.ego_browser.contracts import (
    LIVE_FOR_CLAIM,
)
from agent_remote_server.services.ego_browser.helpers import (
    _canonical_capabilities,
)


class _EgoBrowserAllowlistOperations(_EgoBrowserAdmissionOperations):
    """实现 allowlist 查询和版本化确认。"""

    async def get_allowlist(self, *, user: User, binding_id: UUID) -> dict[str, object]:
        """
        读取 binding 当前文件 allowlist 的元数据。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识

        :return dict[str, object]: allowlist revision、根摘要与文件限制
        """
        binding = await self._repository.get_binding(binding_id)
        if binding is None or binding.user_id != user.id:
            self._error("EGO_BROWSER_BINDING_NOT_FOUND", "The browser binding was not found.", 404)
        return {
            "allowlist_revision": binding.allowlist_revision,
            "roots_digest": binding.allowlist_roots_digest,
            "file_limits": {
                "max_file_bytes": 64 * 1024 * 1024,
                "max_total_bytes": 256 * 1024 * 1024,
                "max_file_count": 32,
            },
        }

    async def confirm_allowlist(
        self,
        *,
        user: User,
        binding_id: UUID,
        payload: EgoBrowserAllowlistConfirmRequest,
        device_id: UUID | None = None,
    ) -> EgoBrowserBinding:
        """
        以 CAS 方式确认新 allowlist 并使旧 generation 暂停。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识
        :param payload (EgoBrowserAllowlistConfirmRequest): allowlist 版本确认请求
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """
        binding, device = await self._owned_binding_device(
            user, binding_id, device_id, for_update=True
        )
        if payload.generation != binding.generation:
            self._generation_error()
        if binding.status not in LIVE_FOR_CLAIM:
            self._error(
                "EGO_BROWSER_STATE_CONFLICT",
                "The browser binding cannot update its allowlist.",
                409,
            )
        if (
            payload.expected_revision != device.allowlist_revision
            or payload.expected_revision != binding.allowlist_revision
        ):
            self._error("EGO_BROWSER_REVISION_CONFLICT", "The allowlist revision has changed.", 409)
        await self._validate_device_pop(
            device=device,
            operation_generation=binding.generation,
            operation="confirm_allowlist",
            binding_id=binding.id,
            payload=payload,
        )
        old_generation = binding.generation
        device.allowlist_revision += 1
        binding.allowlist_revision = device.allowlist_revision
        device.allowlist_roots_digest = payload.roots_digest
        binding.allowlist_roots_digest = payload.roots_digest
        capabilities = _canonical_capabilities(device.capabilities)
        if "ego_browser_file_allowlist_v1" not in capabilities:
            capabilities.append("ego_browser_file_allowlist_v1")
            capabilities.sort()
        capabilities = self._validate_policy_capabilities(
            capabilities,
            allowlist_roots_digest=payload.roots_digest,
            learning_bundle_digest=device.learning_bundle_digest,
        )
        device.capabilities = list(capabilities)
        binding.capabilities = list(capabilities)
        await self._terminalize_generation_requests(
            binding=binding,
            generation=old_generation,
            reason="allowlist_changed",
            actor_user_id=user.id,
        )
        self._advance_generation(binding)
        binding.status = "paused"
        binding.lease_until = None
        binding.lease_health = "expired"
        binding.lease_grace_until = None
        binding.stop_reason = "allowlist_changed"
        await self._enqueue_revocation(binding, old_generation, "allowlist_changed")
        await self._audit(
            user.id,
            "ego_browser_allowlist.updated",
            str(binding.id),
            {"allowlist_revision": device.allowlist_revision, "roots_digest": payload.roots_digest},
        )
        await self._session.commit()
        await self._publish_revocation(binding.id, old_generation)
        return binding
