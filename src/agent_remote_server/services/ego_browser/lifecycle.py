"""处理 ego-browser binding 的连接、续租和终止。"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from agent_remote_server.models import (
    EgoBrowserBinding,
    Node,
    User,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserConnectedRequest,
    EgoBrowserLifecycleRequest,
    EgoBrowserNodeRenewRequest,
    EgoBrowserRenewRequest,
    EgoBrowserResumeRequest,
)
from agent_remote_server.services.ego_browser.contracts import (
    TERMINAL_STATUSES,
)
from agent_remote_server.services.ego_browser.helpers import (
    _as_utc,
    _canonical_capabilities,
    _safe_reason,
    _validate_digest,
)
from agent_remote_server.services.ego_browser.requests import _EgoBrowserRequestOperations


class _EgoBrowserLifecycleOperations(_EgoBrowserRequestOperations):
    """实现 binding 连接后的完整生命周期。"""

    async def connected(
        self,
        *,
        user: User,
        binding_id: UUID,
        payload: EgoBrowserConnectedRequest,
        device_id: UUID | None = None,
    ) -> EgoBrowserBinding:
        """
        校验 Bridge 能力并激活当前 binding。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识
        :param payload (EgoBrowserConnectedRequest): Bridge 连接完成请求
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """
        self._require_enabled()
        binding, device = await self._owned_binding_device(
            user, binding_id, device_id, for_update=True
        )
        self._validate_binding_profile(binding, device)
        _validate_digest(payload.learning_bundle_digest, self._error)
        _validate_digest(payload.allowlist_roots_digest, self._error)
        if payload.generation != binding.generation:
            self._generation_error()
        if binding.status not in {"pending_device", "connecting", "probing_local_browser"}:
            self._error(
                "EGO_BROWSER_STATE_CONFLICT",
                "The binding is not awaiting a Bridge connection.",
                409,
            )
        if not payload.local_browser_ready:
            self._error(
                "EGO_BROWSER_RUNTIME_UNAVAILABLE",
                "The local ego-browser runtime is unavailable.",
                409,
            )
        self._validate_capability_payload(device, payload)
        if (
            payload.allowlist_revision != binding.allowlist_revision
            or payload.allowlist_roots_digest != binding.allowlist_roots_digest
            or payload.learning_bundle_digest != binding.learning_bundle_digest
            or _canonical_capabilities(payload.capabilities)
            != _canonical_capabilities(binding.capabilities)
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The Bridge capability snapshot does not match this binding generation.",
                409,
            )
        await self._validate_device_pop(
            device=device,
            operation_generation=binding.generation,
            operation="connect_binding",
            binding_id=binding.id,
            payload=payload,
        )
        now = self._now()
        binding.status = "active"
        binding.lease_health = "healthy"
        binding.lease_until = min(
            now + timedelta(seconds=self._settings.ego_browser_lease_seconds),
            _as_utc(binding.absolute_ttl_until),
        )
        binding.lease_grace_until = None
        binding.connected_at = now
        binding.local_runtime_version = payload.local_ego_browser_runtime_version
        binding.ego_lite_runtime_version = payload.ego_lite_runtime_version
        binding.skill_version = payload.skill_version
        binding.bridge_protocol_version = payload.bridge_protocol_version
        binding.capabilities = _canonical_capabilities(payload.capabilities)
        device.last_seen_at = now
        self._update_device_from_connected(device, payload)
        await self._audit(
            user.id,
            "ego_browser_binding.activated",
            str(binding.id),
            self._binding_details(binding),
        )
        await self._session.commit()
        return binding

    async def renew(
        self,
        *,
        user: User,
        binding_id: UUID,
        payload: EgoBrowserRenewRequest,
        device_id: UUID | None = None,
    ) -> EgoBrowserBinding:
        """
        续租当前绑定代次。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser 绑定标识
        :param payload (EgoBrowserRenewRequest): Bridge 发起的绑定续租请求
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserBinding: 操作后的 ego-browser 绑定实体
        """
        self._require_enabled()
        binding, device = await self._owned_binding_device(
            user, binding_id, device_id, for_update=True
        )
        self._validate_binding_profile(binding, device)
        _validate_digest(payload.learning_bundle_digest, self._error)
        if payload.generation != binding.generation:
            self._generation_error()
        if binding.status != "active" or binding.lease_health == "expired":
            self._error(
                "EGO_BROWSER_LEASE_EXPIRED", "The browser binding lease is no longer active.", 409
            )
        if (
            payload.allowlist_revision != binding.allowlist_revision
            or payload.learning_bundle_digest != binding.learning_bundle_digest
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH", "The capability revision does not match.", 409
            )
        await self._validate_device_pop(
            device=device,
            operation_generation=binding.generation,
            operation="renew_binding",
            binding_id=binding.id,
            payload=payload,
        )
        now = self._now()
        await self._reject_expired_renewal(binding, now)
        binding.lease_until = min(
            now + timedelta(seconds=self._settings.ego_browser_lease_seconds),
            _as_utc(binding.absolute_ttl_until),
        )
        binding.lease_health = "healthy"
        binding.lease_grace_until = None
        device.last_seen_at = now
        await self._audit(
            user.id, "ego_browser_binding.renewed", str(binding.id), self._binding_details(binding)
        )
        await self._session.commit()
        return binding

    async def renew_for_node(
        self,
        *,
        node: Node,
        binding_id: UUID,
        payload: EgoBrowserNodeRenewRequest,
    ) -> EgoBrowserBinding:
        """
        由已认证 Node 续租 binding，且只接受当前 generation 和能力摘要。

        :param node (Node): 当前操作对应的节点
        :param binding_id (UUID): ego-browser binding 标识
        :param payload (EgoBrowserNodeRenewRequest): Node 发起的 binding 续租请求

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """

        self._require_enabled()
        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None or binding.node_id != node.id:
            self._error(
                "EGO_BROWSER_BINDING_NOT_FOUND",
                "The browser binding was not found.",
                404,
            )
        self._validate_node_capability(node)
        device = await self._repository.get_device(binding.ego_browser_device_id, for_update=True)
        if device is None or device.user_id != binding.user_id:
            self._error(
                "EGO_BROWSER_DEVICE_NOT_FOUND",
                "The ego-browser device was not found.",
                404,
            )
        self._validate_binding_profile(binding, device)
        _validate_digest(payload.learning_bundle_digest, self._error)
        if payload.generation != binding.generation:
            self._generation_error()
        if binding.status != "active" or binding.lease_health == "expired":
            self._error(
                "EGO_BROWSER_LEASE_EXPIRED",
                "The browser binding lease is no longer active.",
                409,
            )
        if (
            payload.allowlist_revision != binding.allowlist_revision
            or payload.learning_bundle_digest != binding.learning_bundle_digest
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The capability revision does not match.",
                409,
            )

        now = self._now()
        await self._reject_expired_renewal(binding, now)

        binding.lease_until = min(
            now + timedelta(seconds=self._settings.ego_browser_lease_seconds),
            _as_utc(binding.absolute_ttl_until),
        )
        binding.lease_health = "healthy"
        binding.lease_grace_until = None
        device.last_seen_at = now
        await self._audit(
            None,
            "ego_browser_binding.renewed",
            str(binding.id),
            self._binding_details(binding),
        )
        await self._session.commit()
        return binding

    async def mark_renewal_failed(
        self, *, binding_id: UUID, generation: int
    ) -> EgoBrowserBinding | None:
        """
        记录续租失败并进入宽限或过期状态。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation

        :return EgoBrowserBinding | None: 匹配的 binding；不存在或无需更新时为 None
        """
        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None or binding.generation != generation or binding.status != "active":
            return None
        now = self._now()
        absolute_ttl_until = _as_utc(binding.absolute_ttl_until)
        grace_until = (
            _as_utc(binding.lease_grace_until)
            if binding.lease_health == "renewal_grace" and binding.lease_grace_until is not None
            else min(
                now
                + timedelta(seconds=self._settings.ego_browser_lease_renew_failure_grace_seconds),
                absolute_ttl_until,
            )
        )
        if now >= absolute_ttl_until or now >= grace_until:
            old_generation = binding.generation
            reason = "absolute_ttl" if now >= absolute_ttl_until else "renewal_grace_expired"
            await self._expire_binding(binding, reason=reason, commit=False)
        elif binding.lease_health != "renewal_grace":
            binding.lease_health = "renewal_grace"
            binding.lease_grace_until = grace_until
            await self._audit(
                None,
                "ego_browser_binding.renewal_failed",
                str(binding.id),
                self._binding_details(binding),
            )
        await self._session.commit()
        if binding.status == "expired":
            await self._publish_revocation(binding.id, old_generation)
        return binding

    async def pause(
        self,
        *,
        user: User,
        binding_id: UUID,
        payload: EgoBrowserLifecycleRequest,
        device_id: UUID | None = None,
    ) -> EgoBrowserBinding:
        """
        暂停绑定并使旧代次失效。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser 绑定标识
        :param payload (EgoBrowserLifecycleRequest): 绑定生命周期操作请求
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserBinding: 操作后的 ego-browser 绑定实体
        """
        binding, device = await self._owned_binding_device(
            user, binding_id, device_id, for_update=True
        )
        if binding.generation != payload.generation:
            self._generation_error()
        if binding.status not in {"active", "connecting", "probing_local_browser"}:
            self._error("EGO_BROWSER_STATE_CONFLICT", "The browser binding cannot be paused.", 409)
        await self._validate_device_pop(
            device=device,
            operation_generation=binding.generation,
            operation="pause_binding",
            binding_id=binding.id,
            payload=payload,
        )
        old_generation = binding.generation
        await self._terminalize_generation_requests(
            binding=binding,
            generation=old_generation,
            reason=payload.reason,
            actor_user_id=user.id,
        )
        self._advance_generation(binding)
        binding.status = "paused"
        binding.lease_until = None
        binding.lease_health = "expired"
        binding.lease_grace_until = None
        binding.stop_reason = _safe_reason(payload.reason)
        await self._enqueue_revocation(binding, old_generation, "pause")
        await self._audit(
            user.id, "ego_browser_binding.paused", str(binding.id), self._binding_details(binding)
        )
        await self._session.commit()
        await self._publish_revocation(binding.id, old_generation)
        return binding

    async def pause_by_user(
        self,
        *,
        user: User,
        binding_id: UUID,
        generation: int,
    ) -> EgoBrowserBinding:
        """
        由所属用户或管理员暂停绑定，不授予任何新能力。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser 绑定标识
        :param generation (int): 目标绑定代次

        :return EgoBrowserBinding: 操作后的 ego-browser 绑定实体
        """

        binding, _device = await self._owned_binding_device(
            user, binding_id, None, for_update=True, allow_admin=True
        )
        if binding.generation != generation:
            self._generation_error()
        if binding.status not in {"active", "connecting", "probing_local_browser"}:
            self._error("EGO_BROWSER_STATE_CONFLICT", "The browser binding cannot be paused.", 409)
        old_generation = binding.generation
        await self._terminalize_generation_requests(
            binding=binding,
            generation=old_generation,
            reason="user_pause",
            actor_user_id=user.id,
        )
        self._advance_generation(binding)
        binding.status = "paused"
        binding.lease_until = None
        binding.lease_health = "expired"
        binding.lease_grace_until = None
        binding.stop_reason = "user_pause"
        await self._enqueue_revocation(binding, old_generation, "pause")
        await self._audit(
            user.id, "ego_browser_binding.paused", str(binding.id), self._binding_details(binding)
        )
        await self._session.commit()
        await self._publish_revocation(binding.id, old_generation)
        return binding

    async def resume(
        self,
        *,
        user: User,
        binding_id: UUID,
        payload: EgoBrowserResumeRequest,
        device_id: UUID | None = None,
    ) -> EgoBrowserBinding:
        """
        在新的 generation 上恢复暂停 binding。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识
        :param payload (EgoBrowserResumeRequest): binding 恢复与用户确认请求
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """
        self._require_enabled()
        binding, device = await self._owned_binding_device(
            user, binding_id, device_id, for_update=True
        )
        self._validate_binding_profile(binding, device)
        if binding.status != "paused" or binding.generation != payload.generation:
            self._generation_error()
        if (
            device.allowlist_revision != payload.allowlist_revision
            or device.learning_bundle_digest != payload.learning_bundle_digest
        ):
            self._error(
                "EGO_BROWSER_CAPABILITY_MISMATCH",
                "The local capability revision does not match.",
                409,
            )
        capabilities = self._validate_policy_capabilities(
            device.capabilities,
            allowlist_roots_digest=device.allowlist_roots_digest,
            learning_bundle_digest=device.learning_bundle_digest,
        )
        await self._validate_device_pop(
            device=device,
            operation_generation=binding.generation,
            operation="resume_binding",
            binding_id=binding.id,
            payload=payload,
        )
        old_generation = binding.generation
        await self._terminalize_generation_requests(
            binding=binding,
            generation=old_generation,
            reason="resume_generation_change",
            actor_user_id=user.id,
        )
        self._advance_generation(binding)
        now = self._now()
        binding.status = "connecting"
        binding.lease_health = "healthy"
        binding.lease_until = min(
            now + timedelta(seconds=self._settings.ego_browser_lease_seconds),
            _as_utc(binding.absolute_ttl_until),
        )
        binding.lease_grace_until = None
        binding.stop_reason = None
        binding.allowlist_revision = device.allowlist_revision
        binding.allowlist_roots_digest = device.allowlist_roots_digest
        binding.learning_bundle_digest = device.learning_bundle_digest
        binding.capabilities = capabilities
        await self._enqueue_revocation(binding, old_generation, "resume_generation_change")
        await self._audit(
            user.id, "ego_browser_binding.resumed", str(binding.id), self._binding_details(binding)
        )
        await self._session.commit()
        await self._publish_revocation(binding.id, old_generation)
        return binding

    async def stop(
        self,
        *,
        user: User,
        binding_id: UUID,
        payload: EgoBrowserLifecycleRequest,
        revoke: bool = False,
        device_id: UUID | None = None,
    ) -> EgoBrowserBinding:
        """
        停止或永久撤销一个 binding。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识
        :param payload (EgoBrowserLifecycleRequest): binding 生命周期操作请求
        :param revoke (bool): 是否将 binding 永久标记为已撤销
        :param device_id (UUID | None): 独立 ego-browser 设备 ID

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """
        binding, device = await self._owned_binding_device(
            user, binding_id, device_id, for_update=True
        )
        if binding.status in TERMINAL_STATUSES and (not revoke or binding.status == "revoked"):
            return binding
        if binding.generation != payload.generation:
            self._generation_error()
        await self._validate_device_pop(
            device=device,
            operation_generation=binding.generation,
            operation="revoke_binding" if revoke else "stop_binding",
            binding_id=binding.id,
            payload=payload,
        )
        old_generation = binding.generation
        invalidated = await self._invalidate_bindings(
            [binding],
            terminal_status="revoked" if revoke else "stopped",
            reason=payload.reason,
            actor_user_id=user.id,
        )
        await self._session.commit()
        if invalidated:
            await self._publish_revocation(binding.id, old_generation)
        return binding

    async def stop_by_user(
        self,
        *,
        user: User,
        binding_id: UUID,
        generation: int,
        reason: str = "user_stop",
        revoke: bool = False,
    ) -> EgoBrowserBinding:
        """
        由所属用户或管理员停止或永久撤销 binding。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        :param reason (str): 写入生命周期与审计记录的原因
        :param revoke (bool): 是否将 binding 永久标记为已撤销

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """

        binding, _device = await self._owned_binding_device(
            user, binding_id, None, for_update=True, allow_admin=True
        )
        if binding.status in TERMINAL_STATUSES and (not revoke or binding.status == "revoked"):
            return binding
        if binding.generation != generation:
            self._generation_error()
        old_generation = binding.generation
        invalidated = await self._invalidate_bindings(
            [binding],
            terminal_status="revoked" if revoke else "stopped",
            reason=reason,
            actor_user_id=user.id,
        )
        await self._session.commit()
        if invalidated:
            await self._publish_revocation(binding.id, old_generation)
        return binding

    async def delete_binding(self, *, user: User, binding_id: UUID) -> None:
        """
        删除已终结且撤销通知已发布的 binding 历史。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser binding 标识

        :raises ApiError: binding 不存在、仍可执行或尚有未完成清理
        """

        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None or (binding.user_id != user.id and user.role != "admin"):
            self._error(
                "EGO_BROWSER_BINDING_NOT_FOUND",
                "The browser binding was not found.",
                404,
            )
        if binding.status not in TERMINAL_STATUSES:
            self._error(
                "EGO_BROWSER_BINDING_DELETE_REQUIRES_TERMINAL",
                "Stop or revoke the browser binding before deleting it.",
                409,
            )
        if await self._repository.has_active_requests(binding.id):
            self._error(
                "EGO_BROWSER_BINDING_DELETE_ACTIVE_REQUESTS",
                "Active browser requests must finish before deleting the binding.",
                409,
            )
        if await self._repository.has_pending_outbox(binding.id):
            self._error(
                "EGO_BROWSER_BINDING_DELETE_PENDING_REVOCATION",
                "The binding revoke notification is still being delivered.",
                409,
            )
        await self._audit(
            user.id,
            "ego_browser_binding.deleted",
            str(binding.id),
            {
                "binding_user_id": str(binding.user_id),
                "device_id": str(binding.ego_browser_device_id),
                "tool_session_id": str(binding.binding_tool_session_id),
                "generation": binding.generation,
                "status": binding.status,
            },
        )
        await self._repository.delete_binding(binding)
        await self._session.commit()

    async def revoke_for_tool_session(
        self,
        *,
        tool_session_id: UUID,
        reason: str,
        commit: bool = True,
        publish: bool = True,
    ) -> int:
        """
        撤销指定工具会话的全部活跃 ego-browser 绑定。

        :param tool_session_id (UUID): 远端工具会话 ID
        :param reason (str): 写入生命周期与审计记录的原因
        :param commit (bool): 是否在操作完成后提交数据库事务
        :param publish (bool): 是否向其他服务实例发布撤销事件

        :return int: 受影响的绑定数量
        """

        bindings = list(
            await self._repository.list_live_for_session(tool_session_id, for_update=True)
        )
        revoked_generations = await self._invalidate_bindings(
            bindings,
            terminal_status="revoked",
            reason=reason,
            actor_user_id=None,
        )
        if revoked_generations and commit:
            await self._session.commit()
        if commit and publish:
            for binding_id, generation in revoked_generations:
                await self._publish_revocation(binding_id, generation)
        return len(revoked_generations)

    async def revoke_for_device(
        self,
        *,
        device_id: UUID,
        reason: str,
        commit: bool = True,
        publish: bool = True,
    ) -> int:
        """
        撤销指定独立 ego-browser 设备的全部 live bindings。

        :param device_id (UUID): 独立 ego-browser 设备 ID
        :param reason (str): 写入生命周期与审计记录的原因
        :param commit (bool): 是否在操作完成后提交数据库事务
        :param publish (bool): 是否向其他服务实例发布撤销事件

        :return int: 受影响的 binding 数量
        """

        bindings = list(await self._repository.list_live_for_device(device_id, for_update=True))
        revoked_generations = await self._invalidate_bindings(
            bindings,
            terminal_status="revoked",
            reason=reason,
            actor_user_id=None,
        )
        if revoked_generations and commit:
            await self._session.commit()
        if commit and publish:
            for binding_id, generation in revoked_generations:
                await self._publish_revocation(binding_id, generation)
        return len(revoked_generations)

    async def revoke_for_user(
        self,
        *,
        user_id: UUID,
        reason: str,
        commit: bool = True,
        publish: bool = True,
    ) -> int:
        """
        撤销用户拥有的全部活跃 ego-browser 绑定。

        :param user_id (UUID): 所属用户 ID
        :param reason (str): 写入生命周期与审计记录的原因
        :param commit (bool): 是否在操作完成后提交数据库事务
        :param publish (bool): 是否向其他服务实例发布撤销事件

        :return int: 受影响的绑定数量
        """

        bindings = list(await self._repository.list_live_for_user(user_id, for_update=True))
        revoked_generations = await self._invalidate_bindings(
            bindings,
            terminal_status="revoked",
            reason=reason,
            actor_user_id=None,
        )
        if revoked_generations and commit:
            await self._session.commit()
        if commit and publish:
            for binding_id, generation in revoked_generations:
                await self._publish_revocation(binding_id, generation)
        return len(revoked_generations)

    async def revoke_for_node(
        self,
        *,
        node_id: UUID,
        reason: str,
        commit: bool = True,
        publish: bool = True,
    ) -> int:
        """
        撤销节点上的全部活跃 ego-browser 绑定。

        :param node_id (UUID): 节点 ID
        :param reason (str): 写入生命周期与审计记录的原因
        :param commit (bool): 是否在操作完成后提交数据库事务
        :param publish (bool): 是否向其他服务实例发布撤销事件

        :return int: 受影响的绑定数量
        """

        bindings = list(await self._repository.list_live_for_node(node_id, for_update=True))
        revoked_generations = await self._invalidate_bindings(
            bindings,
            terminal_status="revoked",
            reason=reason,
            actor_user_id=None,
        )
        if revoked_generations and commit:
            await self._session.commit()
        if commit and publish:
            for binding_id, generation in revoked_generations:
                await self._publish_revocation(binding_id, generation)
        return len(revoked_generations)
