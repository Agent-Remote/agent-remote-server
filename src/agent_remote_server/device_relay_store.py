import asyncio
import json
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID

from redis.asyncio import Redis

from agent_remote_server.config import Settings

DeviceRelayRole = Literal["device", "proxy"]


@dataclass(frozen=True)
class DeviceRelayBinding:
    """
    设备中继完整会话绑定
    """

    user_id: UUID
    device_id: UUID
    tool_session_id: UUID
    device_session_id: UUID
    node_id: UUID
    generation: int


@dataclass(frozen=True)
class DeviceRelayTicketClaims:
    """
    一次性设备中继票据声明
    """

    binding: DeviceRelayBinding
    role: DeviceRelayRole
    credential_id: UUID | None


@dataclass(frozen=True)
class DeviceRelayExchangeResult:
    """
    两端临时公钥交换结果
    """

    status: Literal["waiting", "ready", "already_issued"]
    peer_spki_sha256: str | None = None
    exporter_context: str | None = None


class DeviceRelayStore(Protocol):
    """
    设备中继短期状态存储协议
    """

    async def exchange(
        self,
        *,
        binding: DeviceRelayBinding,
        role: DeviceRelayRole,
        spki_sha256: str,
        exporter_context: str,
        ttl: int,
    ) -> DeviceRelayExchangeResult:
        """
        原子注册本端 SPKI 并仅一次返回完整连接材料

        :param binding (DeviceRelayBinding): 完整设备会话绑定
        :param role (DeviceRelayRole): 当前连接角色
        :param spki_sha256 (str): 本端临时证书 SPKI 摘要
        :param exporter_context (str): 首次配对时使用的候选 exporter 上下文
        :param ttl (int): 短期状态有效秒数

        :return DeviceRelayExchangeResult: 公钥交换结果
        """

    async def issue_ticket(
        self,
        *,
        token_hash: str,
        claims: DeviceRelayTicketClaims,
        ttl: int,
    ) -> None:
        """
        写入一次性中继票据

        :param token_hash (str): 中继票据哈希
        :param claims (DeviceRelayTicketClaims): 中继票据声明
        :param ttl (int): 票据有效秒数
        """

    async def consume_ticket(self, *, token_hash: str) -> DeviceRelayTicketClaims | None:
        """
        原子消费一次性中继票据

        :param token_hash (str): 中继票据哈希

        :return DeviceRelayTicketClaims | None: 中继票据声明
        """

    async def close(self) -> None:
        """
        关闭短期状态存储
        """


_EXCHANGE_SCRIPT = """
local role_pin = ARGV[1] .. '_spki'
local peer_pin = ARGV[2] .. '_spki'
local issued = ARGV[1] .. '_issued'
local existing = redis.call('HGET', KEYS[1], role_pin)
if existing and existing ~= ARGV[3] then
  return {'mismatch'}
end
redis.call('HSET', KEYS[1], role_pin, ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[5])
local peer = redis.call('HGET', KEYS[1], peer_pin)
if not peer then
  return {'waiting'}
end
if redis.call('HEXISTS', KEYS[1], issued) == 1 then
  return {'already_issued'}
end
local secret = redis.call('HGET', KEYS[1], 'exporter_context')
if not secret then
  secret = ARGV[4]
  redis.call('HSET', KEYS[1], 'exporter_context', secret)
end
redis.call('HSET', KEYS[1], issued, '1')
return {'ready', peer, secret}
"""


class RedisDeviceRelayStore:
    """
    Redis 设备中继短期状态存储
    """

    def __init__(self, redis: Redis) -> None:
        """
        初始化 Redis 设备中继短期状态存储

        :param redis (Redis): 异步 Redis 客户端
        """

        self._redis = redis

    async def exchange(
        self,
        *,
        binding: DeviceRelayBinding,
        role: DeviceRelayRole,
        spki_sha256: str,
        exporter_context: str,
        ttl: int,
    ) -> DeviceRelayExchangeResult:
        """
        原子注册本端 SPKI 并仅一次返回完整连接材料

        :param binding (DeviceRelayBinding): 完整设备会话绑定
        :param role (DeviceRelayRole): 当前连接角色
        :param spki_sha256 (str): 本端临时证书 SPKI 摘要
        :param exporter_context (str): 首次配对时使用的候选 exporter 上下文
        :param ttl (int): 短期状态有效秒数

        :return DeviceRelayExchangeResult: 公钥交换结果

        :raises RuntimeError: 中继交换结果为空

        :raises ValueError: 同一代次内设备中继 SPKI 发生变化
        """

        peer: DeviceRelayRole = "proxy" if role == "device" else "device"
        raw = await cast(
            Awaitable[object],
            self._redis.eval(
                _EXCHANGE_SCRIPT,
                1,
                self._exchange_key(binding),
                role,
                peer,
                spki_sha256,
                exporter_context,
                str(ttl),
            ),
        )
        values = cast(list[str], raw)
        if not values:
            raise RuntimeError("empty device relay exchange result")
        if values[0] == "mismatch":
            raise ValueError("device relay SPKI changed within a generation")
        if values[0] == "ready":
            return DeviceRelayExchangeResult(
                status="ready",
                peer_spki_sha256=values[1],
                exporter_context=values[2],
            )
        if values[0] == "already_issued":
            return DeviceRelayExchangeResult(status="already_issued")
        return DeviceRelayExchangeResult(status="waiting")

    async def issue_ticket(
        self,
        *,
        token_hash: str,
        claims: DeviceRelayTicketClaims,
        ttl: int,
    ) -> None:
        """
        写入一次性中继票据

        :param token_hash (str): 中继票据哈希
        :param claims (DeviceRelayTicketClaims): 中继票据声明
        :param ttl (int): 票据有效秒数

        :raises RuntimeError: 中继票据冲突
        """

        payload = json.dumps(
            {
                "binding": {key: str(value) for key, value in asdict(claims.binding).items()},
                "role": claims.role,
                "credential_id": str(claims.credential_id) if claims.credential_id else None,
            }
        )
        created = await self._redis.set(self._ticket_key(token_hash), payload, ex=ttl, nx=True)
        if not created:
            raise RuntimeError("device relay ticket collision")

    async def consume_ticket(self, *, token_hash: str) -> DeviceRelayTicketClaims | None:
        """
        原子消费一次性中继票据

        :param token_hash (str): 中继票据哈希

        :return DeviceRelayTicketClaims | None: 中继票据声明
        """

        payload = await self._redis.getdel(self._ticket_key(token_hash))
        if payload is None:
            return None
        values = json.loads(payload)
        binding = values["binding"]
        return DeviceRelayTicketClaims(
            binding=DeviceRelayBinding(
                user_id=UUID(binding["user_id"]),
                device_id=UUID(binding["device_id"]),
                tool_session_id=UUID(binding["tool_session_id"]),
                device_session_id=UUID(binding["device_session_id"]),
                node_id=UUID(binding["node_id"]),
                generation=int(binding["generation"]),
            ),
            role=values["role"],
            credential_id=(UUID(values["credential_id"]) if values.get("credential_id") else None),
        )

    async def close(self) -> None:
        """
        关闭 Redis 连接
        """

        await self._redis.aclose()

    def _exchange_key(self, binding: DeviceRelayBinding) -> str:
        return (
            f"agent-remote:device-relay-exchange:{binding.device_session_id}:{binding.generation}"
        )

    def _ticket_key(self, token_hash: str) -> str:
        return f"agent-remote:device-relay-ticket:{token_hash}"


class InMemoryDeviceRelayStore:
    """
    测试使用的设备中继短期状态存储
    """

    def __init__(self) -> None:
        """
        初始化测试使用的内存短期状态存储
        """

        self._exchanges: dict[
            tuple[UUID, int],
            tuple[dict[DeviceRelayRole, str], str, set[DeviceRelayRole], datetime],
        ] = {}
        self._tickets: dict[str, tuple[DeviceRelayTicketClaims, datetime]] = {}
        self._lock = asyncio.Lock()

    async def exchange(
        self,
        *,
        binding: DeviceRelayBinding,
        role: DeviceRelayRole,
        spki_sha256: str,
        exporter_context: str,
        ttl: int,
    ) -> DeviceRelayExchangeResult:
        """
        原子注册本端 SPKI 并仅一次返回完整连接材料

        :param binding (DeviceRelayBinding): 完整设备会话绑定
        :param role (DeviceRelayRole): 当前连接角色
        :param spki_sha256 (str): 本端临时证书 SPKI 摘要
        :param exporter_context (str): 首次配对时使用的候选 exporter 上下文
        :param ttl (int): 短期状态有效秒数

        :return DeviceRelayExchangeResult: 公钥交换结果

        :raises ValueError: 同一代次内设备中继 SPKI 发生变化
        """

        key = (binding.device_session_id, binding.generation)
        now = datetime.now(UTC)
        async with self._lock:
            pins, secret, issued, expires_at = self._exchanges.get(
                key, ({}, exporter_context, set(), now + timedelta(seconds=ttl))
            )
            if expires_at <= now:
                pins, secret, issued = {}, exporter_context, set()
                expires_at = now + timedelta(seconds=ttl)
            existing = pins.get(role)
            if existing is not None and existing != spki_sha256:
                raise ValueError("device relay SPKI changed within a generation")
            pins[role] = spki_sha256
            self._exchanges[key] = (pins, secret, issued, expires_at)
            peer: DeviceRelayRole = "proxy" if role == "device" else "device"
            if peer not in pins:
                return DeviceRelayExchangeResult(status="waiting")
            if role in issued:
                return DeviceRelayExchangeResult(status="already_issued")
            issued.add(role)
            return DeviceRelayExchangeResult(
                status="ready",
                peer_spki_sha256=pins[peer],
                exporter_context=secret,
            )

    async def issue_ticket(
        self,
        *,
        token_hash: str,
        claims: DeviceRelayTicketClaims,
        ttl: int,
    ) -> None:
        """
        写入一次性中继票据

        :param token_hash (str): 中继票据哈希
        :param claims (DeviceRelayTicketClaims): 中继票据声明
        :param ttl (int): 票据有效秒数

        :raises RuntimeError: 中继票据冲突
        """

        async with self._lock:
            if token_hash in self._tickets:
                raise RuntimeError("device relay ticket collision")
            self._tickets[token_hash] = (
                claims,
                datetime.now(UTC) + timedelta(seconds=ttl),
            )

    async def consume_ticket(self, *, token_hash: str) -> DeviceRelayTicketClaims | None:
        """
        原子消费一次性中继票据

        :param token_hash (str): 中继票据哈希

        :return DeviceRelayTicketClaims | None: 中继票据声明
        """

        async with self._lock:
            value = self._tickets.pop(token_hash, None)
        if value is None or value[1] <= datetime.now(UTC):
            return None
        return value[0]

    async def close(self) -> None:
        """
        清理内存短期状态
        """

        async with self._lock:
            self._exchanges.clear()
            self._tickets.clear()


def create_device_relay_store(settings: Settings) -> RedisDeviceRelayStore:
    """
    创建生产设备中继短期状态存储

    :param settings (Settings): 应用配置

    :return RedisDeviceRelayStore: Redis 设备中继短期状态存储
    """

    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return RedisDeviceRelayStore(redis)
