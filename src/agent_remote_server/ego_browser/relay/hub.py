"""配对并转发 ego-browser 的本地或分布式密文帧。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import cast
from uuid import UUID, uuid4

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from agent_remote_server.config import Settings
from agent_remote_server.ego_browser.relay.contracts import (
    EgoBrowserRelayRole,
    EgoBrowserRelayTicketClaims,
    EgoBrowserRevocationPublisher,
    FrameValidator,
)
from agent_remote_server.ego_browser.relay.envelope import (
    ApiEnvelopeError,
    parse_outer_envelope,
)
from agent_remote_server.ego_browser.relay.revocation import (
    _distributed_revocation_key,
)
from agent_remote_server.relay.binding import RelayBinding

logger = logging.getLogger("agent_remote_server.ego_browser.relay")

_DISTRIBUTED_FRAME = b"\x01"
_DISTRIBUTED_CLOSE = b"\x02"
_DISTRIBUTED_PRESENCE_TTL_SECONDS = 5
_DISTRIBUTED_POLL_SECONDS = 0.05
_REGISTER_PRESENCE = """
if redis.call('EXISTS', KEYS[2]) == 1 then
  return -1
end
if redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX') then
  return 1
end
return 0
"""
_REFRESH_PRESENCE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_DELETE_PRESENCE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _BindingClosed(Exception):
    pass


class _DistributedStateError(Exception):
    pass


@dataclass
class _Endpoint:
    websocket: WebSocket
    role: EgoBrowserRelayRole
    peer: asyncio.Future[WebSocket]
    started_at: float = dataclass_field(default_factory=time.monotonic)
    frame_count: int = 0
    byte_count: int = 0
    close_code: int = 1011
    metric_counted: bool = False


@dataclass
class _DistributedEndpoint:
    websocket: WebSocket
    role: EgoBrowserRelayRole
    endpoint_id: str
    shutdown: asyncio.Event
    close_code: int = 1008
    started_at: float = dataclass_field(default_factory=time.monotonic)
    frame_count: int = 0
    byte_count: int = 0
    metric_counted: bool = False


class EgoBrowserRelayHub:
    """每个绑定代次仅配对一个 Bridge 和一个包装器。"""

    def __init__(
        self,
        *,
        maximum_frame_bytes: int,
        pair_timeout_seconds: int,
        maximum_bytes_per_second: int,
        maximum_connection_seconds: float,
        revocation_bus: EgoBrowserRevocationPublisher | None = None,
        redis: Redis | None = None,
    ) -> None:
        self._maximum_frame_bytes = maximum_frame_bytes
        self._pair_timeout_seconds = pair_timeout_seconds
        self._maximum_bytes_per_second = maximum_bytes_per_second
        self._maximum_connection_seconds = maximum_connection_seconds
        self._revocation_bus = revocation_bus
        self._redis = redis
        self._pairs: dict[RelayBinding, dict[EgoBrowserRelayRole, _Endpoint]] = {}
        self._distributed_endpoints: dict[
            RelayBinding, dict[EgoBrowserRelayRole, _DistributedEndpoint]
        ] = {}
        self._closed_generations: set[RelayBinding] = set()
        self._closing = False
        self._active_connections: dict[EgoBrowserRelayRole, int] = {
            "bridge": 0,
            "wrapper": 0,
        }
        self._lock = asyncio.Lock()

    async def connect(
        self,
        claims: EgoBrowserRelayTicketClaims,
        websocket: WebSocket,
        validator: FrameValidator,
    ) -> None:
        """
        接受 relay 端点并转发已通过校验的 opaque frame。

        :param claims (EgoBrowserRelayTicketClaims): 一次性 relay 票据声明
        :param websocket (WebSocket): 当前 relay WebSocket 连接
        :param validator (FrameValidator): 逐帧执行 admission 校验的异步回调
        """

        if self._redis is None:
            await self._connect_local(claims, websocket, validator)
            return
        await self._connect_distributed(claims, websocket, validator)

    async def _connect_local(
        self,
        claims: EgoBrowserRelayTicketClaims,
        websocket: WebSocket,
        validator: FrameValidator,
    ) -> None:
        await websocket.accept()
        key = claims.binding.relay_binding
        loop = asyncio.get_running_loop()
        endpoint = _Endpoint(websocket=websocket, role=claims.role, peer=loop.create_future())
        async with self._lock:
            pair = self._pairs.get(key)
            if (
                self._closing
                or key in self._closed_generations
                or (pair is not None and claims.role in pair)
            ):
                duplicate = True
            else:
                duplicate = False
                pair = self._pairs.setdefault(key, {})
                pair[claims.role] = endpoint
                peer_role: EgoBrowserRelayRole = "wrapper" if claims.role == "bridge" else "bridge"
                peer = pair.get(peer_role)
                if peer is not None:
                    endpoint.peer.set_result(peer.websocket)
                    peer.peer.set_result(websocket)
        if duplicate:
            await websocket.close(code=1008)
            return
        self._record_connection_open(endpoint, transport="memory")
        peer_socket: WebSocket | None = None
        close_code = 1011
        try:
            peer_socket = await asyncio.wait_for(
                asyncio.shield(endpoint.peer), timeout=self._pair_timeout_seconds
            )
            try:
                async with asyncio.timeout(self._maximum_connection_seconds):
                    await self._forward_local(endpoint, claims, websocket, peer_socket, validator)
            except TimeoutError:
                close_code = 1008
        except TimeoutError:
            close_code = 1008
        except _BindingClosed:
            close_code = endpoint.close_code
        finally:
            if peer_socket is not None:
                await asyncio.gather(
                    websocket.close(code=close_code),
                    peer_socket.close(code=close_code),
                    return_exceptions=True,
                )
            else:
                await websocket.close(code=close_code)
            await self._remove_local(key, endpoint)
            self._record_connection_close(endpoint, close_code=close_code, transport="memory")

    async def _connect_distributed(
        self,
        claims: EgoBrowserRelayTicketClaims,
        websocket: WebSocket,
        validator: FrameValidator,
    ) -> None:
        redis = self._redis
        if redis is None:
            raise RuntimeError("distributed relay requires Redis")

        await websocket.accept()
        key = claims.binding.relay_binding
        endpoint = _DistributedEndpoint(
            websocket=websocket,
            role=claims.role,
            endpoint_id=uuid4().hex,
            shutdown=asyncio.Event(),
        )
        async with self._lock:
            endpoints = self._distributed_endpoints.get(key)
            rejected = (
                self._closing
                or key in self._closed_generations
                or (endpoints is not None and claims.role in endpoints)
            )
            if not rejected:
                endpoints = self._distributed_endpoints.setdefault(key, {})
                endpoints[claims.role] = endpoint
        if rejected:
            await websocket.close(code=1008)
            return

        channel = self._endpoint_channel(endpoint.endpoint_id)
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        registered = False
        peer_endpoint_id: str | None = None
        close_code = 1011
        try:
            await pubsub.subscribe(channel)
            if endpoint.shutdown.is_set():
                raise _BindingClosed
            registration = await self._register_presence(key, endpoint)
            if registration != 1:
                close_code = 1008
                return
            registered = True
            self._record_connection_open(endpoint, transport="redis")
            peer_endpoint_id = await self._wait_for_distributed_peer(key, endpoint)
            close_code = await self._serve_distributed(
                key,
                claims,
                endpoint,
                peer_endpoint_id,
                pubsub,
                validator,
            )
        except (TimeoutError, _BindingClosed):
            close_code = endpoint.close_code
        except (RedisError, _DistributedStateError):
            close_code = 1011
        finally:
            if peer_endpoint_id is not None:
                packet = _DISTRIBUTED_CLOSE + close_code.to_bytes(2, "big")
                await asyncio.gather(
                    redis.publish(self._endpoint_channel(peer_endpoint_id), packet),
                    return_exceptions=True,
                )
            if registered:
                await asyncio.gather(
                    self._delete_presence(key, endpoint),
                    return_exceptions=True,
                )
            await asyncio.gather(
                pubsub.unsubscribe(channel),
                pubsub.aclose(),
                websocket.close(code=close_code),
                return_exceptions=True,
            )
            await self._remove_distributed(key, endpoint)
            self._record_connection_close(endpoint, close_code=close_code, transport="redis")

    async def close_binding(
        self,
        binding_id: UUID,
        generation: int,
        *,
        code: int = 1008,
        publish: bool = True,
    ) -> None:
        """
        关闭指定绑定代次的本地中继配对。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        :param code (int): WebSocket 关闭状态码
        :param publish (bool): 是否向其他服务实例发布撤销事件
        """

        key = RelayBinding(kind="ego_browser", binding_id=binding_id, generation=generation)
        async with self._lock:
            self._closed_generations.add(key)
            pair = self._pairs.pop(key, None)
            endpoints = list(pair.values()) if pair is not None else []
            for endpoint in endpoints:
                endpoint.close_code = code
                if not endpoint.peer.done():
                    endpoint.peer.set_exception(_BindingClosed())
            distributed_pair = self._distributed_endpoints.pop(key, None)
            distributed_endpoints = (
                list(distributed_pair.values()) if distributed_pair is not None else []
            )
            for distributed_endpoint in distributed_endpoints:
                distributed_endpoint.close_code = code
                distributed_endpoint.shutdown.set()
        await asyncio.gather(
            *(endpoint.websocket.close(code=code) for endpoint in endpoints),
            *(endpoint.websocket.close(code=code) for endpoint in distributed_endpoints),
            return_exceptions=True,
        )
        if publish and self._revocation_bus is not None:
            await self._revocation_bus.publish(binding_id, generation)

    async def close_binding_remote(self, binding_id: UUID, generation: int) -> None:
        """
        响应其他 worker 的撤销通知，只关闭本地连接。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        """

        await self.close_binding(binding_id, generation, publish=False)

    async def close(self) -> None:
        """停止所有本地端点并关闭分布式 relay 连接。"""

        async with self._lock:
            self._closing = True
            keys = set(self._pairs) | set(self._distributed_endpoints)
        await asyncio.gather(
            *(self.close_binding(key.binding_id, key.generation, publish=False) for key in keys),
            return_exceptions=True,
        )
        if self._redis is not None:
            await self._redis.aclose()

    async def _forward_local(
        self,
        endpoint: _Endpoint,
        claims: EgoBrowserRelayTicketClaims,
        source: WebSocket,
        destination: WebSocket,
        validator: FrameValidator,
    ) -> None:
        loop = asyncio.get_running_loop()
        burst = max(self._maximum_bytes_per_second, self._maximum_frame_bytes) * 2
        available = float(burst)
        last_refill = loop.time()
        while True:
            try:
                message = await source.receive()
            except (WebSocketDisconnect, ConnectionError, OSError, RuntimeError):
                return
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if not isinstance(data, bytes) or len(data) > self._maximum_frame_bytes:
                await asyncio.gather(source.close(code=1009), destination.close(code=1009))
                return
            try:
                envelope = parse_outer_envelope(data, maximum_bytes=self._maximum_frame_bytes)
                await validator(claims, data, envelope)
            except (ValueError, ApiEnvelopeError):
                await asyncio.gather(source.close(code=1008), destination.close(code=1008))
                return
            now = loop.time()
            available = min(
                float(burst),
                available + max(0.0, now - last_refill) * self._maximum_bytes_per_second,
            )
            last_refill = now
            if len(data) > available:
                await asyncio.gather(source.close(code=1008), destination.close(code=1008))
                return
            available -= len(data)
            endpoint.frame_count += 1
            endpoint.byte_count += len(data)
            try:
                await destination.send_bytes(data)
            except (WebSocketDisconnect, ConnectionError, OSError, RuntimeError):
                return

    async def _wait_for_distributed_peer(
        self,
        key: RelayBinding,
        endpoint: _DistributedEndpoint,
    ) -> str:
        redis = self._redis
        if redis is None:
            raise RuntimeError("distributed relay requires Redis")
        peer_role: EgoBrowserRelayRole = "wrapper" if endpoint.role == "bridge" else "bridge"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._pair_timeout_seconds
        next_refresh = loop.time() + _DISTRIBUTED_PRESENCE_TTL_SECONDS / 3
        while True:
            if endpoint.shutdown.is_set():
                raise _BindingClosed
            now = loop.time()
            if now >= deadline:
                raise TimeoutError
            if now >= next_refresh:
                if not await self._refresh_presence(key, endpoint):
                    raise _DistributedStateError
                next_refresh = now + _DISTRIBUTED_PRESENCE_TTL_SECONDS / 3
            revoked, peer_endpoint_id = await self._distributed_peer_state(key, peer_role)
            if revoked:
                endpoint.close_code = 1008
                raise _BindingClosed
            if peer_endpoint_id is not None:
                return peer_endpoint_id
            try:
                await asyncio.wait_for(
                    endpoint.shutdown.wait(),
                    timeout=min(_DISTRIBUTED_POLL_SECONDS, deadline - now),
                )
            except TimeoutError:
                continue

    async def _serve_distributed(
        self,
        key: RelayBinding,
        claims: EgoBrowserRelayTicketClaims,
        endpoint: _DistributedEndpoint,
        peer_endpoint_id: str,
        pubsub: PubSub,
        validator: FrameValidator,
    ) -> int:
        tasks = {
            asyncio.create_task(
                self._websocket_to_redis(
                    endpoint,
                    claims,
                    endpoint.websocket,
                    peer_endpoint_id,
                    validator,
                )
            ),
            asyncio.create_task(self._redis_to_websocket(endpoint.websocket, pubsub)),
            asyncio.create_task(self._keep_distributed_pair(key, endpoint, peer_endpoint_id)),
            asyncio.create_task(self._wait_for_shutdown(endpoint)),
        }
        done, pending = await asyncio.wait(
            tasks,
            timeout=self._maximum_connection_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return 1008
        close_codes = [task.result() for task in done]
        if endpoint.shutdown.is_set():
            return endpoint.close_code
        for preferred_code in (1009, 1008, 1011):
            if preferred_code in close_codes:
                return preferred_code
        return close_codes[0]

    async def _websocket_to_redis(
        self,
        endpoint: _DistributedEndpoint,
        claims: EgoBrowserRelayTicketClaims,
        source: WebSocket,
        peer_endpoint_id: str,
        validator: FrameValidator,
    ) -> int:
        redis = self._redis
        if redis is None:
            raise RuntimeError("distributed relay requires Redis")
        loop = asyncio.get_running_loop()
        burst = max(self._maximum_bytes_per_second, self._maximum_frame_bytes) * 2
        available = float(burst)
        last_refill = loop.time()
        destination = self._endpoint_channel(peer_endpoint_id)
        while True:
            try:
                message = await source.receive()
            except (WebSocketDisconnect, ConnectionError, OSError, RuntimeError):
                return 1011
            if message.get("type") == "websocket.disconnect":
                return 1011
            data = message.get("bytes")
            if not isinstance(data, bytes) or len(data) > self._maximum_frame_bytes:
                return 1009
            try:
                envelope = parse_outer_envelope(data, maximum_bytes=self._maximum_frame_bytes)
                await validator(claims, data, envelope)
            except (ValueError, ApiEnvelopeError):
                return 1008
            now = loop.time()
            available = min(
                float(burst),
                available + max(0.0, now - last_refill) * self._maximum_bytes_per_second,
            )
            last_refill = now
            if len(data) > available:
                return 1008
            available -= len(data)
            endpoint.frame_count += 1
            endpoint.byte_count += len(data)
            subscribers = await redis.publish(destination, _DISTRIBUTED_FRAME + data)
            if not isinstance(subscribers, int) or subscribers < 1:
                return 1011

    async def _redis_to_websocket(self, destination: WebSocket, pubsub: PubSub) -> int:
        while True:
            message = await pubsub.get_message(timeout=1.0)
            if message is None or message.get("type") != "message":
                continue
            payload = message.get("data")
            if isinstance(payload, str):
                payload = payload.encode()
            if not isinstance(payload, bytes):
                return 1011
            if payload.startswith(_DISTRIBUTED_FRAME):
                frame = payload[1:]
                if not frame or len(frame) > self._maximum_frame_bytes:
                    return 1011
                try:
                    await destination.send_bytes(frame)
                except (WebSocketDisconnect, ConnectionError, OSError, RuntimeError):
                    return 1011
                continue
            if payload.startswith(_DISTRIBUTED_CLOSE) and len(payload) == 3:
                close_code = int.from_bytes(payload[1:], "big")
                return close_code if 1000 <= close_code <= 4999 else 1011
            return 1011

    async def _keep_distributed_pair(
        self,
        key: RelayBinding,
        endpoint: _DistributedEndpoint,
        peer_endpoint_id: str,
    ) -> int:
        peer_role: EgoBrowserRelayRole = "wrapper" if endpoint.role == "bridge" else "bridge"
        loop = asyncio.get_running_loop()
        next_refresh = loop.time() + _DISTRIBUTED_PRESENCE_TTL_SECONDS / 3
        while True:
            await asyncio.sleep(_DISTRIBUTED_POLL_SECONDS)
            if endpoint.shutdown.is_set():
                return endpoint.close_code
            revoked, current_peer_endpoint_id = await self._distributed_peer_state(key, peer_role)
            if revoked:
                return 1008
            if current_peer_endpoint_id != peer_endpoint_id:
                return 1011
            now = loop.time()
            if now >= next_refresh:
                if not await self._refresh_presence(key, endpoint):
                    return 1011
                next_refresh = now + _DISTRIBUTED_PRESENCE_TTL_SECONDS / 3

    @staticmethod
    async def _wait_for_shutdown(endpoint: _DistributedEndpoint) -> int:
        await endpoint.shutdown.wait()
        return endpoint.close_code

    async def _refresh_presence(
        self,
        key: RelayBinding,
        endpoint: _DistributedEndpoint,
    ) -> bool:
        redis = self._redis
        if redis is None:
            raise RuntimeError("distributed relay requires Redis")
        result = await cast(
            Awaitable[object],
            redis.eval(
                _REFRESH_PRESENCE,
                1,
                self._presence_key(key, endpoint.role),
                endpoint.endpoint_id,
                str(_DISTRIBUTED_PRESENCE_TTL_SECONDS),
            ),
        )
        return result == 1

    async def _register_presence(
        self,
        key: RelayBinding,
        endpoint: _DistributedEndpoint,
    ) -> int:
        redis = self._redis
        if redis is None:
            raise RuntimeError("distributed relay requires Redis")
        result = await cast(
            Awaitable[object],
            redis.eval(
                _REGISTER_PRESENCE,
                2,
                self._presence_key(key, endpoint.role),
                _distributed_revocation_key(key.binding_id, key.generation),
                endpoint.endpoint_id,
                str(_DISTRIBUTED_PRESENCE_TTL_SECONDS),
            ),
        )
        if not isinstance(result, int):
            raise _DistributedStateError
        return result

    async def _distributed_peer_state(
        self,
        key: RelayBinding,
        peer_role: EgoBrowserRelayRole,
    ) -> tuple[bool, str | None]:
        redis = self._redis
        if redis is None:
            raise RuntimeError("distributed relay requires Redis")
        revoked, peer_value = await redis.mget(
            _distributed_revocation_key(key.binding_id, key.generation),
            self._presence_key(key, peer_role),
        )
        return (
            revoked is not None,
            self._decode_endpoint_id(peer_value) if peer_value is not None else None,
        )

    async def _delete_presence(
        self,
        key: RelayBinding,
        endpoint: _DistributedEndpoint,
    ) -> None:
        redis = self._redis
        if redis is None:
            return
        await cast(
            Awaitable[object],
            redis.eval(
                _DELETE_PRESENCE,
                1,
                self._presence_key(key, endpoint.role),
                endpoint.endpoint_id,
            ),
        )

    async def _remove_local(self, key: RelayBinding, endpoint: _Endpoint) -> None:
        async with self._lock:
            pair = self._pairs.get(key)
            if pair is None or pair.get(endpoint.role) is not endpoint:
                return
            del pair[endpoint.role]
            if not pair:
                del self._pairs[key]

    async def _remove_distributed(
        self,
        key: RelayBinding,
        endpoint: _DistributedEndpoint,
    ) -> None:
        async with self._lock:
            pair = self._distributed_endpoints.get(key)
            if pair is None or pair.get(endpoint.role) is not endpoint:
                return
            del pair[endpoint.role]
            if not pair:
                del self._distributed_endpoints[key]

    def _record_connection_open(
        self,
        endpoint: _Endpoint | _DistributedEndpoint,
        *,
        transport: str,
    ) -> None:
        endpoint.metric_counted = True
        self._active_connections[endpoint.role] += 1
        logger.info(
            "ego browser relay metric",
            extra={
                "metric_name": "ego_browser_bridge_connections",
                "metric_value": self._active_connections[endpoint.role],
                "metric_unit": "connections",
                "metric_operation": "opened",
                "relay_role": endpoint.role,
                "relay_transport": transport,
            },
        )

    def _record_connection_close(
        self,
        endpoint: _Endpoint | _DistributedEndpoint,
        *,
        close_code: int,
        transport: str,
    ) -> None:
        if not endpoint.metric_counted:
            return
        endpoint.metric_counted = False
        self._active_connections[endpoint.role] = max(
            0,
            self._active_connections[endpoint.role] - 1,
        )
        logger.info(
            "ego browser relay metric",
            extra={
                "metric_name": "ego_browser_bridge_connections",
                "metric_value": self._active_connections[endpoint.role],
                "metric_unit": "connections",
                "metric_operation": "closed",
                "relay_role": endpoint.role,
                "relay_transport": transport,
            },
        )
        logger.info(
            "ego browser relay metric",
            extra={
                "metric_name": "ego_browser_bytes_total",
                "metric_value": endpoint.byte_count,
                "metric_unit": "bytes",
                "metric_operation": "closed",
                "metric_status": self._close_status(close_code),
                "metric_direction": "request" if endpoint.role == "wrapper" else "response",
                "relay_role": endpoint.role,
                "relay_transport": transport,
                "frame_count": endpoint.frame_count,
                "close_code": close_code,
                "duration_ms": round((time.monotonic() - endpoint.started_at) * 1000, 3),
            },
        )

    @staticmethod
    def _close_status(close_code: int) -> str:
        if close_code == 1000:
            return "completed"
        if close_code == 1008:
            return "rejected"
        if close_code == 1009:
            return "frame_limit"
        return "transport_error"

    @staticmethod
    def _presence_key(key: RelayBinding, role: EgoBrowserRelayRole) -> str:
        return f"agent-remote:ego-browser:relay-presence:{key.binding_id}:{key.generation}:{role}"

    @staticmethod
    def _endpoint_channel(endpoint_id: str) -> str:
        return f"agent-remote:ego-browser:relay-endpoint:{endpoint_id}"

    @staticmethod
    def _decode_endpoint_id(value: object) -> str:
        if isinstance(value, bytes):
            value = value.decode("ascii")
        if not isinstance(value, str):
            raise _DistributedStateError
        try:
            endpoint_id = UUID(value).hex
        except ValueError as exc:
            raise _DistributedStateError from exc
        if endpoint_id != value:
            raise _DistributedStateError
        return endpoint_id


def create_ego_browser_relay_hub(
    settings: Settings,
    revocation_bus: EgoBrowserRevocationPublisher | None = None,
) -> EgoBrowserRelayHub:
    """
    按照应用策略创建 ego-browser 中继中心。

    :param settings (Settings): 应用配置
    :param revocation_bus (EgoBrowserRevocationPublisher | None): ego-browser generation 撤销发布器

    :return EgoBrowserRelayHub: 按当前部署模式创建的 relay 连接中心
    """

    redis = None
    if not settings.database_url.startswith("sqlite"):
        redis = Redis.from_url(settings.redis_url, decode_responses=False)
    return EgoBrowserRelayHub(
        maximum_frame_bytes=settings.ego_browser_relay_max_frame_bytes,
        pair_timeout_seconds=settings.ego_browser_relay_pair_timeout_seconds,
        maximum_bytes_per_second=settings.ego_browser_relay_max_bytes_per_second,
        maximum_connection_seconds=settings.ego_browser_relay_max_connection_seconds,
        revocation_bus=revocation_bus,
        redis=redis,
    )
