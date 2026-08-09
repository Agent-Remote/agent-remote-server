import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect

from agent_remote_server.device_relay_revocation import DeviceRelayRevocationPublisher
from agent_remote_server.device_relay_store import DeviceRelayRole, DeviceRelayTicketClaims

logger = logging.getLogger(__name__)


class _RelayBindingClosed(Exception):
    """当前 relay binding 已被控制面撤销。"""


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
        revocation_bus: DeviceRelayRevocationPublisher | None = None,
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
        self._revocation_bus = revocation_bus
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

        peer: WebSocket | None = None
        relay_close_code = 1011
        try:
            peer = await asyncio.wait_for(
                asyncio.shield(endpoint.peer),
                timeout=self._pair_timeout_seconds,
            )
            try:
                async with asyncio.timeout(self._maximum_connection_seconds):
                    await self._forward(key, endpoint.role, websocket, peer)
            except TimeoutError:
                relay_close_code = 1008
                logger.warning(
                    "device_relay_closed session=%s generation=%s role=%s reason=%s",
                    key[0],
                    key[1],
                    endpoint.role,
                    "connection_timeout",
                    extra={
                        "device_session_id": str(key[0]),
                        "generation": key[1],
                        "relay_role": endpoint.role,
                        "relay_reason": "connection_timeout",
                    },
                )
        except (TimeoutError, _RelayBindingClosed):
            logger.warning(
                "device_relay_closed session=%s generation=%s role=%s reason=%s",
                key[0],
                key[1],
                endpoint.role,
                "pair_timeout_or_revoked",
                extra={
                    "device_session_id": str(key[0]),
                    "generation": key[1],
                    "relay_role": endpoint.role,
                    "relay_reason": "pair_timeout_or_revoked",
                },
            )
            await websocket.close(code=1008)
        finally:
            if peer is not None:
                # 任一方向结束后，本代一次性材料都无法再配对；双端关闭可让存活端及时轮换代次。
                await asyncio.gather(
                    websocket.close(code=relay_close_code),
                    peer.close(code=relay_close_code),
                    return_exceptions=True,
                )
            await self._remove(key, endpoint)

    async def close_binding(
        self,
        device_session_id: UUID,
        generation: int,
        *,
        code: int = 1008,
        publish: bool = True,
    ) -> None:
        """
        主动关闭指定 device session generation 的两端 relay

        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 被撤销的连接代次
        :param code (int): WebSocket 关闭码
        """

        key = (device_session_id, generation)
        async with self._lock:
            pair = self._pairs.pop(key, None)
            endpoints = list(pair.values()) if pair is not None else []
            for endpoint in endpoints:
                if not endpoint.peer.done():
                    endpoint.peer.set_exception(_RelayBindingClosed())
        if endpoints:
            await asyncio.gather(
                *(endpoint.websocket.close(code=code) for endpoint in endpoints),
                return_exceptions=True,
            )
        if publish and self._revocation_bus is not None:
            await self._revocation_bus.publish(device_session_id, generation)

    async def close_binding_remote(self, device_session_id: UUID, generation: int) -> None:
        """
        响应其他 worker 的撤销通知，只关闭本地 relay 且不再次广播

        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 被撤销的连接代次
        """

        await self.close_binding(device_session_id, generation, publish=False)

    async def _forward(
        self,
        key: tuple[UUID, int],
        role: DeviceRelayRole,
        source: WebSocket,
        destination: WebSocket,
    ) -> None:
        loop = asyncio.get_running_loop()
        window_started = loop.time()
        window_bytes = 0
        total_bytes = 0
        frame_count = 0
        while True:
            try:
                message = await source.receive()
            except WebSocketDisconnect as error:
                self._log_forward_end(
                    key,
                    role,
                    "source_disconnected",
                    frame_count,
                    total_bytes,
                    error.code,
                )
                return
            if message["type"] == "websocket.disconnect":
                self._log_forward_end(
                    key,
                    role,
                    "source_disconnected",
                    frame_count,
                    total_bytes,
                    message.get("code"),
                )
                return
            data = message.get("bytes")
            if not isinstance(data, bytes):
                self._log_forward_end(key, role, "non_binary_frame", frame_count, total_bytes, 1003)
                await source.close(code=1003)
                return
            if len(data) > self._maximum_frame_bytes:
                self._log_forward_end(key, role, "frame_limit", frame_count, total_bytes, 1009)
                await source.close(code=1009)
                await destination.close(code=1009)
                return
            now = loop.time()
            if now - window_started >= 1:
                window_started = now
                window_bytes = 0
            window_bytes += len(data)
            if window_bytes > self._maximum_bytes_per_second:
                self._log_forward_end(key, role, "rate_limit", frame_count, total_bytes, 1008)
                await source.close(code=1008)
                await destination.close(code=1008)
                return
            frame_count += 1
            total_bytes += len(data)
            try:
                await destination.send_bytes(data)
            except WebSocketDisconnect as error:
                self._log_forward_end(
                    key,
                    role,
                    "destination_disconnected",
                    frame_count,
                    total_bytes,
                    error.code,
                )
                return

    @staticmethod
    def _log_forward_end(
        key: tuple[UUID, int],
        role: DeviceRelayRole,
        reason: str,
        frame_count: int,
        total_bytes: int,
        close_code: object,
    ) -> None:
        logger.warning(
            (
                "device_relay_forward_ended session=%s generation=%s role=%s "
                "reason=%s frames=%s bytes=%s close_code=%s"
            ),
            key[0],
            key[1],
            role,
            reason,
            frame_count,
            total_bytes,
            close_code,
            extra={
                "device_session_id": str(key[0]),
                "generation": key[1],
                "relay_role": role,
                "relay_reason": reason,
                "relay_frame_count": frame_count,
                "relay_total_bytes": total_bytes,
                "relay_close_code": close_code,
            },
        )

    async def _remove(self, key: tuple[UUID, int], endpoint: _RelayEndpoint) -> None:
        async with self._lock:
            pair = self._pairs.get(key)
            if pair is None or pair.get(endpoint.role) is not endpoint:
                return
            del pair[endpoint.role]
            if not pair:
                del self._pairs[key]
