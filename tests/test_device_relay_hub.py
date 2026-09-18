"""
验证设备中继中心行为。
"""

import asyncio
from typing import cast
from uuid import UUID, uuid4

from fastapi import WebSocket

from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.device_control.relay_revocation import DeviceRelayRevocationPublisher
from agent_remote_server.device_control.relay_store import (
    DeviceRelayBinding,
    DeviceRelayRole,
    DeviceRelayTicketClaims,
)


class _RecordingRevocationBus:
    """
    定义记录撤销总线。
    """

    def __init__(self) -> None:
        """
        初始化记录撤销总线。
        """
        self.events: list[tuple[UUID, int]] = []

    async def publish(self, device_session_id: UUID, generation: int) -> None:
        """
        发布记录的事件。

        :param device_session_id (UUID): 设备会话 ID
        :param generation (int): 代次
        """
        self.events.append((device_session_id, generation))


class _FakeWebSocket:
    """
    定义测试替身 Web 套接字。
    """

    def __init__(self, *, send_error: Exception | None = None) -> None:
        """
        初始化测试替身 Web 套接字。

        :param send_error (Exception | None): 发送错误
        """
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.close_codes: list[int] = []
        self.accepted = False
        self.send_error = send_error

    async def accept(self) -> None:
        """
        接受测试 WebSocket 连接。
        """
        self.accepted = True

    async def receive(self) -> dict[str, object]:
        """
        返回接收。

        :return dict[str, object]: 接收
        """
        return await self.messages.get()

    async def send_bytes(self, data: bytes) -> None:
        """
        发送字节。

        :param data (bytes): 数据
        """
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        """
        关闭当前连接。

        :param code (int): 代码
        """
        self.close_codes.append(code)
        self.messages.put_nowait({"type": "websocket.disconnect"})


async def test_device_relay_hub_enforces_frame_and_rate_limits_for_both_peers() -> None:
    """
    验证超帧和超速密文会同时关闭设备端与代理端。
    """

    async def run_case(
        *,
        frame_limit: int,
        byte_rate: int,
        payloads: list[bytes],
        code: int,
        forwarded: list[bytes],
    ) -> None:
        """
        运行用例。

        :param frame_limit (int): frame 限制
        :param byte_rate (int): byte 速率
        :param payloads (list[bytes]): 待发送载荷
        :param code (int): 代码
        :param forwarded (list[bytes]): 已转发载荷
        """
        hub = DeviceRelayHub(
            maximum_frame_bytes=frame_limit,
            pair_timeout_seconds=1,
            maximum_bytes_per_second=byte_rate,
            maximum_connection_seconds=1,
        )
        device = _FakeWebSocket()
        proxy = _FakeWebSocket()
        for payload in payloads:
            device.messages.put_nowait({"type": "websocket.receive", "bytes": payload})
        binding = _binding()
        await asyncio.gather(
            hub.connect(_claims(binding, "device"), cast(WebSocket, device)),
            hub.connect(_claims(binding, "proxy"), cast(WebSocket, proxy)),
        )
        assert device.accepted and proxy.accepted
        assert code in device.close_codes
        assert code in proxy.close_codes
        assert proxy.sent == forwarded

    await run_case(
        frame_limit=4,
        byte_rate=8,
        payloads=[b"12345"],
        code=1009,
        forwarded=[],
    )
    await run_case(
        frame_limit=4,
        byte_rate=8,
        payloads=[b"1234", b"5678", b"9012", b"3456", b"7"],
        code=1008,
        forwarded=[b"1234", b"5678", b"9012", b"3456"],
    )


async def test_device_relay_hub_allows_a_bounded_multiframe_burst() -> None:
    """
    验证图片响应可在持续速率限制内使用有界突发容量。
    """

    hub = DeviceRelayHub(
        maximum_frame_bytes=4,
        pair_timeout_seconds=1,
        maximum_bytes_per_second=8,
        maximum_connection_seconds=1,
    )
    device = _FakeWebSocket()
    proxy = _FakeWebSocket()
    for payload in [b"1234", b"5678", b"9012"]:
        device.messages.put_nowait({"type": "websocket.receive", "bytes": payload})
    device.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})
    binding = _binding()

    await asyncio.gather(
        hub.connect(_claims(binding, "device"), cast(WebSocket, device)),
        hub.connect(_claims(binding, "proxy"), cast(WebSocket, proxy)),
    )

    assert proxy.sent == [b"1234", b"5678", b"9012"]
    assert 1008 not in device.close_codes
    assert 1008 not in proxy.close_codes


async def test_device_relay_hub_expires_both_peers_after_the_connection_limit() -> None:
    """
    验证配对后的固定生命周期到期会同时关闭两端。
    """

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
    """
    验证代理断开会关闭设备端，触发设备轮换 relay generation。
    """

    await _assert_peer_closed_after_disconnect(disconnected_role="proxy")


async def test_device_relay_hub_closes_proxy_when_device_disconnects() -> None:
    """
    验证设备断开会关闭代理端，不留下单边僵尸 relay。
    """

    await _assert_peer_closed_after_disconnect(disconnected_role="device")


async def test_device_relay_hub_broadcasts_local_close_but_not_remote_close() -> None:
    """
    验证本地撤销会广播，其他 worker 的通知不会形成消息回环。
    """

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
    """
    创建测试绑定。

    :return DeviceRelayBinding: 绑定
    """
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
    """
    断言对等节点已关闭之后断开连接。

    :param disconnected_role (DeviceRelayRole): disconnected 角色
    """
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


async def test_device_relay_hub_cleans_up_when_peer_send_breaks() -> None:
    """
    传输层 broken pipe 不应冒泡成未处理 websocket 异常。
    """

    hub = DeviceRelayHub(
        maximum_frame_bytes=16,
        pair_timeout_seconds=1,
        maximum_bytes_per_second=16,
        maximum_connection_seconds=1,
    )
    device = _FakeWebSocket()
    proxy = _FakeWebSocket(send_error=BrokenPipeError("peer closed"))
    device.messages.put_nowait({"type": "websocket.receive", "bytes": b"frame"})
    binding = _binding()

    await asyncio.gather(
        hub.connect(_claims(binding, "device"), cast(WebSocket, device)),
        hub.connect(_claims(binding, "proxy"), cast(WebSocket, proxy)),
    )

    assert device.close_codes
    assert proxy.close_codes
    assert proxy.sent == []


def _claims(binding: DeviceRelayBinding, role: DeviceRelayRole) -> DeviceRelayTicketClaims:
    """
    构造中继票据声明。

    :param binding (DeviceRelayBinding): 绑定
    :param role (DeviceRelayRole): 角色
    :return DeviceRelayTicketClaims: 中继票据声明
    """
    return DeviceRelayTicketClaims(
        binding=binding,
        role=role,
        credential_id=uuid4() if role == "device" else None,
    )
