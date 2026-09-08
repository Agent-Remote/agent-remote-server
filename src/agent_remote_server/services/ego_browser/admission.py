"""校验 ego-browser relay 外层信封并收敛请求账本。"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from agent_remote_server.ego_browser.relay import (
    EGO_BROWSER_PROTOCOL,
    EgoBrowserRelayTicketClaims,
)
from agent_remote_server.models import (
    EgoBrowserRequestLedger,
)
from agent_remote_server.services.ego_browser.helpers import (
    _as_utc,
    _decode_encryption_public_key,
    _validate_outer_key_wrap,
)
from agent_remote_server.services.ego_browser.relay import _EgoBrowserRelayOperations


class _EgoBrowserAdmissionOperations(_EgoBrowserRelayOperations):
    """实现不解密业务内容的 relay admission。"""

    async def admit_outer_envelope(
        self,
        *,
        claims: EgoBrowserRelayTicketClaims,
        envelope: dict[str, object],
    ) -> None:
        """
        认证路由元数据并记录重放状态，但不解密内层内容。

        :param claims (EgoBrowserRelayTicketClaims): 一次性 relay 票据声明
        :param envelope (dict[str, object]): 已解析的外层信封元数据

        :raises ValueError: 信封身份、租约、密钥包装、顺序或重放状态未通过 admission
        """

        if (
            envelope.get("protocol") != EGO_BROWSER_PROTOCOL
            or envelope.get("channel") != "ego_browser_bridge"
            or envelope.get("relay_binding_kind") != "ego_browser"
        ):
            raise ValueError("outer_identity")
        try:
            binding_id = UUID(str(envelope["binding_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("binding_id") from exc
        if binding_id != claims.binding.binding_id:
            raise ValueError("binding_identity")
        if envelope.get("generation") != claims.binding.generation:
            raise ValueError("generation")
        direction = envelope.get("direction")
        expected_direction = "request" if claims.role == "wrapper" else "response"
        if direction != expected_direction:
            raise ValueError("direction")
        message_type_value = envelope.get("type")
        if (direction == "request" and message_type_value not in {"execute", "cancel"}) or (
            direction == "response" and message_type_value != "execute_result"
        ):
            raise ValueError("type")
        _validate_outer_key_wrap(envelope)
        binding = await self._repository.get_binding(binding_id, for_update=True)
        if binding is None or binding.generation != claims.binding.generation:
            raise ValueError("binding")
        device = await self._repository.get_device(binding.ego_browser_device_id)
        if device is None or device.encryption_public_key is None:
            raise ValueError("encryption_key")
        if (
            binding.user_id != claims.binding.user_id
            or binding.ego_browser_device_id != claims.binding.ego_browser_device_id
            or binding.binding_tool_session_id != claims.binding.tool_session_id
            or binding.node_id != claims.binding.node_id
            or device.user_id != binding.user_id
            or device.status != "active"
            or not self._binding_profile_is_current(binding, device)
        ):
            raise ValueError("binding_identity")
        _decode_encryption_public_key(device.encryption_public_key)
        now = self._now()
        is_cleanup = message_type_value in {"cancel", "execute_result"}
        if binding.status != "active":
            raise ValueError("binding_inactive")
        if _as_utc(binding.absolute_ttl_until) <= now:
            raise ValueError("lease")
        if binding.lease_health == "healthy":
            if binding.lease_until is None or _as_utc(binding.lease_until) <= now:
                raise ValueError("lease")
        elif binding.lease_health == "renewal_grace" and is_cleanup:
            if binding.lease_grace_until is None or _as_utc(binding.lease_grace_until) <= now:
                raise ValueError("lease")
        else:
            raise ValueError("binding_inactive")
        if message_type_value == "execute":
            if binding.lease_until is None:
                raise ValueError("lease")
            if _as_utc(binding.lease_until) - now < timedelta(
                seconds=self._settings.ego_browser_lease_admission_min_remaining_seconds
            ):
                raise ValueError("lease_renewal_required")
        request_id_value = envelope["request_id"]
        if (
            not isinstance(request_id_value, str)
            or not request_id_value
            or len(request_id_value) > 128
            or any(ord(char) < 0x20 for char in request_id_value)
        ):
            raise ValueError("request_id")
        request_id = request_id_value
        sequence_value = envelope["sequence"]
        payload_bytes_value = envelope["payload_bytes"]
        if (
            isinstance(sequence_value, bool)
            or not isinstance(sequence_value, int)
            or sequence_value < 1
            or isinstance(payload_bytes_value, bool)
            or not isinstance(payload_bytes_value, int)
            or payload_bytes_value < 1
        ):
            raise ValueError("frame_metadata")
        sequence = sequence_value
        message_type = str(message_type_value)
        payload_bytes = payload_bytes_value
        request: EgoBrowserRequestLedger | None = None
        if message_type == "cancel":
            request = await self._repository.get_ledger(
                binding_id=binding.id,
                generation=binding.generation,
                direction="request",
                request_id=request_id,
                sequence=sequence,
                for_update=True,
            )
            if request is None or request.message_type != "execute":
                raise ValueError("cancel_without_request")
            if request.status == "completed":
                # 终态响应已先提交；继续转发通过认证的迟到取消，可让 Bridge 独立确认目标已消失。
                await self._session.commit()
                return
            if request.status == "cancelled":
                raise ValueError("replay")
            if request.status not in {"accepted", "cancel_requested"}:
                raise ValueError("cancel_request_state")
            request.status = "cancelled"
            await self._audit(
                None,
                "ego_browser_execute.cancelled",
                str(binding.id),
                {
                    "generation": binding.generation,
                    "request_id": request_id,
                    "sequence": sequence,
                    "direction": direction,
                    "payload_bytes": payload_bytes,
                },
            )
            await self._session.commit()
            return
        if direction == "response":
            request = await self._repository.get_ledger(
                binding_id=binding.id,
                generation=binding.generation,
                direction="request",
                request_id=request_id,
                sequence=sequence,
                for_update=True,
            )
            if request is None or request.message_type != "execute":
                raise ValueError("response_without_request")
        else:
            maximum_sequence = await self._repository.max_sequence(
                binding_id=binding.id,
                generation=binding.generation,
                direction="request",
            )
            if maximum_sequence is not None and sequence <= maximum_sequence:
                raise ValueError("sequence")
        ledger = EgoBrowserRequestLedger(
            binding_id=binding.id,
            generation=binding.generation,
            request_id=request_id,
            sequence=sequence,
            direction=direction,
            message_type=message_type,
            payload_bytes=payload_bytes,
            status="completed" if direction == "response" else "accepted",
        )
        try:
            async with self._session.begin_nested():
                await self._repository.add_ledger(ledger)
                audit_action: str | None = "ego_browser_execute.accepted"
                if direction == "response":
                    if request is None:
                        raise ValueError("response_without_request")
                    if request.status != "cancelled":
                        request.status = "completed"
                        audit_action = "ego_browser_execute.completed"
                    else:
                        audit_action = None
                if audit_action is not None:
                    await self._audit(
                        None,
                        audit_action,
                        str(binding.id),
                        {
                            "generation": binding.generation,
                            "request_id": request_id,
                            "sequence": sequence,
                            "direction": direction,
                            "payload_bytes": payload_bytes,
                        },
                    )
            await self._session.commit()
        except IntegrityError as exc:
            raise ValueError("replay") from exc
