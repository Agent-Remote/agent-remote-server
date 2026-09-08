"""处理 ego-browser 请求取消与 Node 结果收敛。"""

from __future__ import annotations

from uuid import UUID

from agent_remote_server.models import (
    EgoBrowserBinding,
    EgoBrowserRequestLedger,
    NodeTask,
    User,
)
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserCancelRequest,
)
from agent_remote_server.services.ego_browser.bindings import _EgoBrowserBindingOperations
from agent_remote_server.services.ego_browser.helpers import (
    _cancel_task_identity,
)


class _EgoBrowserRequestOperations(_EgoBrowserBindingOperations):
    """实现请求查询、取消与失败后的暂停处理。"""

    async def list_active_requests(
        self, *, user: User, binding_id: UUID
    ) -> list[EgoBrowserRequestLedger]:
        """
        列出用户可取消的当前请求元数据。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser 绑定标识

        :return list[EgoBrowserRequestLedger]: 当前仍可取消的请求账本列表
        """

        self._require_enabled()
        await self.expire_due()
        await self.get_binding(user=user, binding_id=binding_id)
        return list(await self._repository.list_active_requests(binding_id=binding_id))

    async def cancel_request(
        self,
        *,
        user: User,
        binding_id: UUID,
        request_id: str,
        payload: EgoBrowserCancelRequest,
    ) -> EgoBrowserRequestLedger:
        """
        持久化精确请求取消并幂等下发 Node 任务。

        :param user (User): 当前操作用户
        :param binding_id (UUID): ego-browser 绑定标识
        :param request_id (str): 外层浏览器请求 ID
        :param payload (EgoBrowserCancelRequest): 浏览器请求取消参数

        :return EgoBrowserRequestLedger: 已加入数据库会话或更新后的请求账本实体
        """

        self._require_enabled()
        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None or (binding.user_id != user.id and user.role != "admin"):
            self._error("EGO_BROWSER_BINDING_NOT_FOUND", "The browser binding was not found.", 404)
        request = await self._repository.get_ledger(
            binding_id=binding.id,
            generation=payload.generation,
            direction="request",
            request_id=request_id,
            sequence=payload.sequence,
            for_update=True,
        )
        if request is None or request.message_type != "execute":
            self._error("EGO_BROWSER_REQUEST_NOT_FOUND", "The browser request was not found.", 404)
        if request.status in {"completed", "cancelled", "rejected"}:
            await self._session.commit()
            return request
        if binding.generation != request.generation or binding.status != "active":
            self._error(
                "EGO_BROWSER_REQUEST_NOT_ACTIVE",
                "The browser request is no longer active.",
                409,
            )
        if request.status == "accepted":
            request.status = "cancel_requested"
            await self._repository.add_task(
                NodeTask(
                    task_id=f"cancel_ego_browser_request:{request.id}",
                    node_id=binding.node_id,
                    task_type="cancel_ego_browser_request",
                    status="pending",
                    payload={
                        "binding_id": str(binding.id),
                        "generation": request.generation,
                        "request_id": request.request_id,
                        "sequence": request.sequence,
                    },
                    retry_count=0,
                )
            )
            await self._audit(
                user.id,
                "ego_browser_execute.cancel_requested",
                str(binding.id),
                {
                    "generation": request.generation,
                    "request_id": request.request_id,
                    "sequence": request.sequence,
                },
            )
        await self._session.commit()
        return request

    async def reconcile_cancel_task(
        self,
        *,
        task: NodeTask,
        result: dict[str, object],
        succeeded: bool,
    ) -> None:
        """
        根据 Node 的有界取消结果收敛请求账本和绑定代次。

        :param task (NodeTask): 待核对的 Node 取消任务
        :param result (dict[str, object]): Node 返回的任务执行结果
        :param succeeded (bool): Node 任务是否执行成功
        """

        identity = _cancel_task_identity(task)
        if identity is None:
            return
        binding_id, generation, request_id, sequence = identity
        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None:
            return
        request = await self._repository.get_ledger(
            binding_id=binding_id,
            generation=generation,
            direction="request",
            request_id=request_id,
            sequence=sequence,
            for_update=True,
        )
        if request is None or request.message_type != "execute":
            return
        if request.status in {"completed", "cancelled", "rejected"}:
            return

        cancellation_confirmed = (
            succeeded
            and result.get("status") == "cancellation_completed"
            and result.get("request_active") is True
            and result.get("server_terminal_observed") is True
            and set(result)
            == {
                "status",
                "request_active",
                "server_terminal_observed",
            }
        )
        if cancellation_confirmed:
            request.status = "cancelled"
            await self._audit(
                None,
                "ego_browser_execute.cancelled",
                str(binding.id),
                {
                    "generation": generation,
                    "request_id": request_id,
                    "sequence": sequence,
                    "reason": "node_terminal_observed",
                },
            )
            return

        if binding.generation != generation or binding.status != "active":
            request.status = "cancelled"
            await self._audit(
                None,
                "ego_browser_execute.cancelled",
                str(binding.id),
                {
                    "generation": generation,
                    "request_id": request_id,
                    "sequence": sequence,
                    "reason": "generation_inactive",
                },
            )
            return

        await self._pause_unconfirmed_request_cancel(binding)

    async def _pause_unconfirmed_request_cancel(self, binding: EgoBrowserBinding) -> None:
        """取消结果无法证明时撤销整个 generation，并保持 binding 可显式恢复。"""

        old_generation = binding.generation
        await self._terminalize_generation_requests(
            binding=binding,
            generation=old_generation,
            reason="request_cancel_unconfirmed",
            actor_user_id=None,
        )
        self._advance_generation(binding)
        binding.status = "paused"
        binding.lease_until = None
        binding.lease_health = "expired"
        binding.lease_grace_until = None
        binding.stop_reason = "request_cancel_unconfirmed"
        await self._enqueue_revocation(binding, old_generation, "request_cancel_unconfirmed")
        await self._audit(
            None,
            "ego_browser_binding.paused",
            str(binding.id),
            self._binding_details(binding),
        )
