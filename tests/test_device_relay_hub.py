import asyncio
from typing import cast
from uuid import UUID, uuid4

from fastapi import WebSocket

from agent_remote_server.device_relay_hub import DeviceRelayHub
from agent_remote_server.device_relay_revocation import DeviceRelayRevocationPublisher
from agent_remote_server.device_relay_store import (
    DeviceRelayBinding,
    DeviceRelayRole,
    DeviceRelayTicketClaims,
)


class _RecordingRevocationBus:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, int]] = []

    async def publish(self, device_session_id: UUID, generation: int) -> None:
        self.events.append((device_session_id, generation))


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


async def test_device_relay_hub_closes_device_when_proxy_disconnects() -> None:
    """验证代理断开会关闭设备端，触发设备轮换 relay generation。"""

    await _assert_peer_closed_after_disconnect(disconnected_role="proxy")


async def test_device_relay_hub_closes_proxy_when_device_disconnects() -> None:
    """验证设备断开会关闭代理端，不留下单边僵尸 relay。"""

    await _assert_peer_closed_after_disconnect(disconnected_role="device")


async def test_device_relay_hub_broadcasts_local_close_but_not_remote_close() -> None:
    """验证本地撤销会广播，其他 worker 的通知不会形成消息回环。"""

    bus = _RecordingRevocationBus()
    hub = DeviceRelayHub(
        maximum_frame_bytes=16,
        pair_timeout_seconds=1,
        maximum_bytes_per_second=16,
        maximum_connection_seconds=1,
        revocation_bus=cast(DeviceRelayRevocationPublisher, bus),
    )
    binding = _binding()
    await hub.close_binding(binding.device_session_id, binding.generation)
    assert bus.events == [(binding.device_session_id, binding.generation)]
    await hub.close_binding_remote(binding.device_session_id, binding.generation)
    assert bus.events == [(binding.device_session_id, binding.generation)]


def _binding() -> DeviceRelayBinding:
    return DeviceRelayBinding(
        user_id=uuid4(),
        device_id=uuid4(),
        tool_session_id=uuid4(),
        device_session_id=uuid4(),
        node_id=uuid4(),
        generation=1,
    )


async def _assert_peer_closed_after_disconnect(
    *,
    disconnected_role: DeviceRelayRole,
) -> None:
    hub = DeviceRelayHub(
        maximum_frame_bytes=16,
        pair_timeout_seconds=1,
        maximum_bytes_per_second=16,
        maximum_connection_seconds=1,
    )
    device = _FakeWebSocket()
    proxy = _FakeWebSocket()
    disconnected = proxy if disconnected_role == "proxy" else device
    peer = device if disconnected_role == "proxy" else proxy
    disconnected.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})
    binding = _binding()

    await asyncio.gather(
        hub.connect(_claims(binding, "device"), cast(WebSocket, device)),
        hub.connect(_claims(binding, "proxy"), cast(WebSocket, proxy)),
    )

    assert disconnected.accepted and peer.accepted
    assert 1011 in peer.close_codes


def _claims(binding: DeviceRelayBinding, role: DeviceRelayRole) -> DeviceRelayTicketClaims:
    return DeviceRelayTicketClaims(
        binding=binding,
        role=role,
        credential_id=uuid4() if role == "device" else None,
    )
