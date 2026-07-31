import asyncio
from dataclasses import dataclass
from uuid import UUID

from fastapi import WebSocket

from agent_remote_server.device_relay_store import DeviceRelayRole, DeviceRelayTicketClaims


@dataclass
class _RelayEndpoint:
    websocket: WebSocket
    role: DeviceRelayRole
    peer: asyncio.Future[WebSocket]


class DeviceRelayHub:
    """
    仅在内存中配对并转发设备端到端加密帧
    """

    def __init__(
        self,
        *,
        maximum_frame_bytes: int,
        pair_timeout_seconds: int,
        maximum_bytes_per_second: int,
        maximum_connection_seconds: float,
    ) -> None:
        """
        初始化设备密文中继中心

        :param maximum_frame_bytes (int): 单个密文帧最大字节数
        :param pair_timeout_seconds (int): 等待对端连接的最长秒数
        :param maximum_bytes_per_second (int): 每个方向每秒允许的最大密文字节数
        :param maximum_connection_seconds (float): 配对后单次中继连接最长秒数
        """

        self._maximum_frame_bytes = maximum_frame_bytes
        self._pair_timeout_seconds = pair_timeout_seconds
        self._maximum_bytes_per_second = maximum_bytes_per_second
        self._maximum_connection_seconds = maximum_connection_seconds
        self._pairs: dict[tuple[UUID, int], dict[DeviceRelayRole, _RelayEndpoint]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, claims: DeviceRelayTicketClaims, websocket: WebSocket) -> None:
        """
        接受已消费票据的角色并转发其密文帧

        :param claims (DeviceRelayTicketClaims): 已验证的一次性票据声明
        :param websocket (WebSocket): 当前角色 WebSocket
        """

        await websocket.accept()
        key = (claims.binding.device_session_id, claims.binding.generation)
        loop = asyncio.get_running_loop()
        endpoint = _RelayEndpoint(
            websocket=websocket,
            role=claims.role,
            peer=loop.create_future(),
        )
        async with self._lock:
            pair = self._pairs.setdefault(key, {})
            if claims.role in pair:
                await websocket.close(code=1008)
                return
            pair[claims.role] = endpoint
            peer_role: DeviceRelayRole = "proxy" if claims.role == "device" else "device"
            peer_endpoint = pair.get(peer_role)
            if peer_endpoint is not None:
                endpoint.peer.set_result(peer_endpoint.websocket)
                peer_endpoint.peer.set_result(websocket)

        try:
            peer = await asyncio.wait_for(
                asyncio.shield(endpoint.peer),
                timeout=self._pair_timeout_seconds,
            )
            try:
                async with asyncio.timeout(self._maximum_connection_seconds):
                    await self._forward(websocket, peer)
            except TimeoutError:
                await websocket.close(code=1008)
                await peer.close(code=1008)
        except TimeoutError:
            await websocket.close(code=1008)
        finally:
            await self._remove(key, endpoint)

    async def _forward(self, source: WebSocket, destination: WebSocket) -> None:
        loop = asyncio.get_running_loop()
        window_started = loop.time()
        window_bytes = 0
        while True:
            message = await source.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if not isinstance(data, bytes):
                await source.close(code=1003)
                return
            if len(data) > self._maximum_frame_bytes:
                await source.close(code=1009)
                await destination.close(code=1009)
                return
            now = loop.time()
            if now - window_started >= 1:
                window_started = now
                window_bytes = 0
            window_bytes += len(data)
            if window_bytes > self._maximum_bytes_per_second:
                await source.close(code=1008)
                await destination.close(code=1008)
                return
            await destination.send_bytes(data)

    async def _remove(self, key: tuple[UUID, int], endpoint: _RelayEndpoint) -> None:
        async with self._lock:
            pair = self._pairs.get(key)
            if pair is None or pair.get(endpoint.role) is not endpoint:
                return
            del pair[endpoint.role]
            if not pair:
                del self._pairs[key]
