import asyncio
from typing import cast
from uuid import uuid4

from fastapi import WebSocket

from agent_remote_server.device_relay_hub import DeviceRelayHub
from agent_remote_server.device_relay_store import (
    DeviceRelayBinding,
    DeviceRelayRole,
    DeviceRelayTicketClaims,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.close_codes: list[int] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, object]:
        return await self.messages.get()

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)
        self.messages.put_nowait({"type": "websocket.disconnect"})


async def test_device_relay_hub_enforces_frame_and_rate_limits_for_both_peers() -> None:
    """验证超帧和超速密文会同时关闭设备端与代理端。"""

    async def run_case(*, frame_limit: int, byte_rate: int, payload: bytes, code: int) -> None:
        hub = DeviceRelayHub(
            maximum_frame_bytes=frame_limit,
            pair_timeout_seconds=1,
            maximum_bytes_per_second=byte_rate,
            maximum_connection_seconds=1,
        )
        device = _FakeWebSocket()
        proxy = _FakeWebSocket()
        device.messages.put_nowait({"type": "websocket.receive", "bytes": payload})
        binding = _binding()
        await asyncio.gather(
            hub.connect(_claims(binding, "device"), cast(WebSocket, device)),
            hub.connect(_claims(binding, "proxy"), cast(WebSocket, proxy)),
        )
        assert device.accepted and proxy.accepted
        assert code in device.close_codes
        assert code in proxy.close_codes
        assert proxy.sent == []

    await run_case(frame_limit=4, byte_rate=8, payload=b"12345", code=1009)
    await run_case(frame_limit=8, byte_rate=4, payload=b"12345", code=1008)


async def test_device_relay_hub_expires_both_peers_after_the_connection_limit() -> None:
    """验证配对后的固定生命周期到期会同时关闭两端。"""

    hub = DeviceRelayHub(
        maximum_frame_bytes=16,
        pair_timeout_seconds=1,
        maximum_bytes_per_second=16,
        maximum_connection_seconds=0.01,
    )
    device = _FakeWebSocket()
    proxy = _FakeWebSocket()
    binding = _binding()
    await asyncio.gather(
        hub.connect(_claims(binding, "device"), cast(WebSocket, device)),
        hub.connect(_claims(binding, "proxy"), cast(WebSocket, proxy)),
    )

    assert 1008 in device.close_codes
    assert 1008 in proxy.close_codes


def _binding() -> DeviceRelayBinding:
    return DeviceRelayBinding(
        user_id=uuid4(),
        device_id=uuid4(),
        tool_session_id=uuid4(),
        device_session_id=uuid4(),
        node_id=uuid4(),
        generation=1,
    )


def _claims(binding: DeviceRelayBinding, role: DeviceRelayRole) -> DeviceRelayTicketClaims:
    return DeviceRelayTicketClaims(
        binding=binding,
        role=role,
        credential_id=uuid4() if role == "device" else None,
    )
