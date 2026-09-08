"""签发并校验 ego-browser 中继票据。"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from agent_remote_server.ego_browser.relay import (
    EgoBrowserRelayBinding,
    EgoBrowserRelayRole,
    EgoBrowserRelayTicketClaims,
)
from agent_remote_server.models import (
    Node,
    User,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserRelayTicketRequest,
)
from agent_remote_server.security import create_opaque_token, hash_token
from agent_remote_server.services.ego_browser.contracts import (
    EgoBrowserRelayTicketResult,
)
from agent_remote_server.services.ego_browser.helpers import (
    _as_utc,
    _decode_encryption_public_key,
)
from agent_remote_server.services.ego_browser.lifecycle import _EgoBrowserLifecycleOperations


class _EgoBrowserRelayOperations(_EgoBrowserLifecycleOperations):
    """实现中继票据的签发与时效校验。"""

    async def relay_claims_are_current(self, claims: EgoBrowserRelayTicketClaims) -> bool:
        """
        重新校验已消费票据的身份、租约和发布策略。

        :param claims (EgoBrowserRelayTicketClaims): 一次性中继票据声明

        :return bool: 票据声明是否仍对应可用的当前代次
        """

        binding = await self._repository.get_binding(claims.binding.binding_id)
        if binding is None or binding.lease_until is None:
            return False
        device = await self._repository.get_device(binding.ego_browser_device_id)
        if device is None:
            return False
        expected = claims.binding
        return (
            binding.user_id == expected.user_id
            and binding.ego_browser_device_id == expected.ego_browser_device_id
            and binding.binding_tool_session_id == expected.tool_session_id
            and binding.node_id == expected.node_id
            and binding.generation == expected.generation
            and binding.status == "active"
            and binding.lease_health == "healthy"
            and _as_utc(binding.lease_until) > self._now()
            and self._binding_profile_is_current(binding, device)
        )

    async def issue_relay_ticket(
        self,
        *,
        binding_id: UUID,
        payload: EgoBrowserRelayTicketRequest,
        role: EgoBrowserRelayRole,
        user: User | None = None,
        node: Node | None = None,
        device_id: UUID | None = None,
    ) -> EgoBrowserRelayTicketResult:
        """
        为已授权 Bridge 或 Node 封装器签发一次性中继票据。

        :param binding_id (UUID): ego-browser 绑定标识
        :param payload (EgoBrowserRelayTicketRequest): 一次性中继票据签发请求
        :param role (EgoBrowserRelayRole): 申请票据的中继端角色
        :param user (User | None): 当前操作用户
        :param node (Node | None): 当前操作对应的节点
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserRelayTicketResult: 一次性票据及其代次和过期时间
        """
        self._require_enabled()
        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None:
            self._error("EGO_BROWSER_BINDING_NOT_FOUND", "The browser binding was not found.", 404)
        if role != payload.role:
            self._error("EGO_BROWSER_RELAY_ROLE_INVALID", "The relay role is invalid.", 403)
        if payload.generation != binding.generation:
            self._generation_error()
        if binding.status != "active" or binding.lease_health != "healthy":
            self._error("EGO_BROWSER_RELAY_UNAVAILABLE", "The browser binding is not active.", 409)
        now = self._now()
        if binding.lease_until is None or _as_utc(binding.lease_until) <= now:
            self._error(
                "EGO_BROWSER_RELAY_UNAVAILABLE",
                "The browser binding lease has expired.",
                409,
            )
        device = await self._repository.get_device(binding.ego_browser_device_id)
        if device is None:
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND", "The ego-browser device was not found.", 404
            )
        self._validate_binding_profile(binding, device)
        if role == "wrapper":
            if node is None:
                self._error(
                    "EGO_BROWSER_RELAY_FORBIDDEN", "The wrapper node is not authorized.", 403
                )
            self._validate_node_capability(node)
        if device.encryption_public_key is None:
            self._error(
                "EGO_BROWSER_ENCRYPTION_KEY_REQUIRED",
                "The browser device has no registered encryption key.",
                409,
            )
        _decode_encryption_public_key(device.encryption_public_key)
        if role == "bridge":
            if (
                user is None
                or user.id != binding.user_id
                or device_id is None
                or device_id != binding.ego_browser_device_id
                or payload.ego_browser_device_id != binding.ego_browser_device_id
            ):
                self._error(
                    "EGO_BROWSER_RELAY_FORBIDDEN", "The bridge identity is not authorized.", 403
                )
            await self._validate_device_pop(
                device=device,
                operation_generation=binding.generation,
                operation="issue_relay_ticket",
                binding_id=binding.id,
                payload=payload,
            )
        else:
            if node is None or node.id != binding.node_id:
                self._error(
                    "EGO_BROWSER_RELAY_FORBIDDEN", "The wrapper node is not authorized.", 403
                )
        if self._relay_store is None:
            self._error("EGO_BROWSER_RELAY_UNAVAILABLE", "The relay store is unavailable.", 503)
        token = create_opaque_token("egbr")
        expires_at = min(
            now + timedelta(seconds=self._settings.ego_browser_relay_ticket_ttl_seconds),
            _as_utc(binding.lease_until),
        )
        ttl = max(1, int((expires_at - now).total_seconds()))
        claims = EgoBrowserRelayTicketClaims(
            binding=EgoBrowserRelayBinding(
                user_id=binding.user_id,
                ego_browser_device_id=binding.ego_browser_device_id,
                tool_session_id=binding.binding_tool_session_id,
                binding_id=binding.id,
                node_id=binding.node_id,
                generation=binding.generation,
            ),
            role=role,
        )
        await self._relay_store.issue_ticket(
            token_hash=hash_token(self._settings.secret_key, token),
            claims=claims,
            ttl=ttl,
        )
        return EgoBrowserRelayTicketResult(
            role=role, generation=binding.generation, relay_ticket=token, expires_at=expires_at
        )
