"""存储 ego-browser relay ticket 和设备 PoP challenge。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from agent_remote_server.config import Settings
from agent_remote_server.ego_browser.relay.contracts import (
    EgoBrowserProofChallengeClaims,
    EgoBrowserRelayBinding,
    EgoBrowserRelayTicketClaims,
)


@dataclass(frozen=True)
class EgoBrowserRelayTicket:
    """带过期时间的内存票据记录。"""

    claims: EgoBrowserRelayTicketClaims
    expires_at: datetime


@dataclass(frozen=True)
class EgoBrowserProofChallenge:
    """带过期时间的内存 PoP challenge 记录。"""

    claims: EgoBrowserProofChallengeClaims
    expires_at: datetime


class EgoBrowserRelayStore(Protocol):
    """ego-browser relay 短期票据存储协议。"""

    async def issue_ticket(
        self, *, token_hash: str, claims: EgoBrowserRelayTicketClaims, ttl: int
    ) -> None:
        """
        签发一个短期的一次性 relay ticket。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param claims (EgoBrowserRelayTicketClaims): 一次性 relay 票据声明
        :param ttl (int): 短期状态的有效秒数

        :raises RuntimeError: token hash 对应的未过期 relay 票据已经存在
        """

    async def consume_ticket(self, *, token_hash: str) -> EgoBrowserRelayTicketClaims | None:
        """
        原子消费一次性 relay ticket。

        :param token_hash (str): 一次性凭据的 keyed hash

        :return EgoBrowserRelayTicketClaims | None: 已消费的 relay 票据声明；不存在或过期时为 None
        """

    async def issue_proof_challenge(
        self, *, token_hash: str, claims: EgoBrowserProofChallengeClaims, ttl: int
    ) -> None:
        """
        签发一个短期的一次性设备 PoP challenge。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param claims (EgoBrowserProofChallengeClaims): 设备 PoP challenge 声明
        :param ttl (int): 短期状态的有效秒数

        :raises RuntimeError: token hash 对应的未过期 PoP challenge 已经存在
        """

    async def consume_proof_challenge(
        self, *, token_hash: str
    ) -> EgoBrowserProofChallengeClaims | None:
        """
        原子消费一次性设备 PoP challenge。

        :param token_hash (str): 一次性凭据的 keyed hash

        :return EgoBrowserProofChallengeClaims | None: 已消费的声明；不存在或过期时为 None
        """

    async def close(self) -> None:
        """关闭票据存储并释放连接。"""


class RedisEgoBrowserRelayStore:
    """使用独立 key namespace 的 Redis 一次性票据存储。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def issue_ticket(
        self, *, token_hash: str, claims: EgoBrowserRelayTicketClaims, ttl: int
    ) -> None:
        """
        写入带独立命名空间的一次性 Redis ticket。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param claims (EgoBrowserRelayTicketClaims): 一次性 relay 票据声明
        :param ttl (int): 短期状态的有效秒数

        :raises RuntimeError: token hash 对应的未过期 relay 票据已经存在
        """
        payload = json.dumps(
            {
                "binding": {key: str(value) for key, value in asdict(claims.binding).items()},
                "role": claims.role,
            },
            separators=(",", ":"),
        )
        created = await self._redis.set(self._key(token_hash), payload, ex=ttl, nx=True)
        if not created:
            raise RuntimeError("ego-browser relay ticket collision")

    async def consume_ticket(self, *, token_hash: str) -> EgoBrowserRelayTicketClaims | None:
        """
        从 Redis 原子取出并删除 ticket。

        :param token_hash (str): 一次性凭据的 keyed hash

        :return EgoBrowserRelayTicketClaims | None: 已消费的 relay 票据声明；不存在或过期时为 None
        """
        payload = await self._redis.getdel(self._key(token_hash))
        if payload is None:
            return None
        try:
            value = json.loads(payload)
            binding = value["binding"]
            role = value["role"]
            if role not in {"bridge", "wrapper"}:
                return None
            return EgoBrowserRelayTicketClaims(
                binding=EgoBrowserRelayBinding(
                    user_id=UUID(binding["user_id"]),
                    ego_browser_device_id=UUID(binding["ego_browser_device_id"]),
                    tool_session_id=UUID(binding["tool_session_id"]),
                    binding_id=UUID(binding["binding_id"]),
                    node_id=UUID(binding["node_id"]),
                    generation=int(binding["generation"]),
                ),
                role=role,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def issue_proof_challenge(
        self, *, token_hash: str, claims: EgoBrowserProofChallengeClaims, ttl: int
    ) -> None:
        """
        写入独立命名空间的单次 PoP challenge。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param claims (EgoBrowserProofChallengeClaims): 设备 PoP challenge 声明
        :param ttl (int): 短期状态的有效秒数

        :raises RuntimeError: token hash 对应的未过期 PoP challenge 已经存在
        """

        payload = json.dumps(
            {
                "user_id": str(claims.user_id),
                "ego_browser_device_id": str(claims.ego_browser_device_id),
                "operation": claims.operation,
                "generation": claims.generation,
                "binding_id": str(claims.binding_id) if claims.binding_id is not None else None,
            },
            separators=(",", ":"),
        )
        created = await self._redis.set(self._proof_key(token_hash), payload, ex=ttl, nx=True)
        if not created:
            raise RuntimeError("ego-browser proof challenge collision")

    async def consume_proof_challenge(
        self, *, token_hash: str
    ) -> EgoBrowserProofChallengeClaims | None:
        """
        从 Redis 原子取出并删除 PoP challenge。

        :param token_hash (str): 一次性凭据的 keyed hash

        :return EgoBrowserProofChallengeClaims | None: 已消费的声明；不存在或过期时为 None
        """

        payload = await self._redis.getdel(self._proof_key(token_hash))
        if payload is None:
            return None
        try:
            value = json.loads(payload)
            binding_id = value["binding_id"]
            if binding_id is not None:
                binding_id = UUID(binding_id)
            return EgoBrowserProofChallengeClaims(
                user_id=UUID(value["user_id"]),
                ego_browser_device_id=UUID(value["ego_browser_device_id"]),
                operation=str(value["operation"]),
                generation=int(value["generation"]),
                binding_id=binding_id,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def close(self) -> None:
        """关闭 Redis 票据连接。"""
        await self._redis.aclose()

    @staticmethod
    def _key(token_hash: str) -> str:
        return f"agent-remote:ego-browser:relay-ticket:{token_hash}"

    @staticmethod
    def _proof_key(token_hash: str) -> str:
        return f"agent-remote:ego-browser:pop-challenge:{token_hash}"


class InMemoryEgoBrowserRelayStore:
    """SQLite 测试和单进程运行使用的确定性内存票据存储。"""

    def __init__(self) -> None:
        self._tickets: dict[str, EgoBrowserRelayTicket] = {}
        self._proof_challenges: dict[str, EgoBrowserProofChallenge] = {}
        self._lock = asyncio.Lock()

    async def issue_ticket(
        self, *, token_hash: str, claims: EgoBrowserRelayTicketClaims, ttl: int
    ) -> None:
        """
        在进程内写入带 TTL 的一次性 ticket。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param claims (EgoBrowserRelayTicketClaims): 一次性 relay 票据声明
        :param ttl (int): 短期状态的有效秒数

        :raises RuntimeError: token hash 对应的未过期 relay 票据已经存在
        """
        async with self._lock:
            existing = self._tickets.get(token_hash)
            if existing is not None and existing.expires_at > datetime.now(UTC):
                raise RuntimeError("ego-browser relay ticket collision")
            self._tickets[token_hash] = EgoBrowserRelayTicket(
                claims=claims,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            )

    async def consume_ticket(self, *, token_hash: str) -> EgoBrowserRelayTicketClaims | None:
        """
        在进程内原子消费一次性 ticket。

        :param token_hash (str): 一次性凭据的 keyed hash

        :return EgoBrowserRelayTicketClaims | None: 已消费的 relay 票据声明；不存在或过期时为 None
        """
        async with self._lock:
            ticket = self._tickets.pop(token_hash, None)
        if ticket is None or ticket.expires_at <= datetime.now(UTC):
            return None
        return ticket.claims

    async def issue_proof_challenge(
        self, *, token_hash: str, claims: EgoBrowserProofChallengeClaims, ttl: int
    ) -> None:
        """
        在进程内写入带 TTL 的一次性 PoP challenge。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param claims (EgoBrowserProofChallengeClaims): 设备 PoP challenge 声明
        :param ttl (int): 短期状态的有效秒数

        :raises RuntimeError: token hash 对应的未过期 PoP challenge 已经存在
        """

        async with self._lock:
            existing = self._proof_challenges.get(token_hash)
            if existing is not None and existing.expires_at > datetime.now(UTC):
                raise RuntimeError("ego-browser proof challenge collision")
            self._proof_challenges[token_hash] = EgoBrowserProofChallenge(
                claims=claims,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            )

    async def consume_proof_challenge(
        self, *, token_hash: str
    ) -> EgoBrowserProofChallengeClaims | None:
        """
        在进程内原子消费一次性 PoP challenge。

        :param token_hash (str): 一次性凭据的 keyed hash

        :return EgoBrowserProofChallengeClaims | None: 已消费的声明；不存在或过期时为 None
        """

        async with self._lock:
            challenge = self._proof_challenges.pop(token_hash, None)
        if challenge is None or challenge.expires_at <= datetime.now(UTC):
            return None
        return challenge.claims

    async def close(self) -> None:
        """清空进程内 ticket。"""
        async with self._lock:
            self._tickets.clear()
            self._proof_challenges.clear()


def create_ego_browser_relay_store(settings: Settings) -> EgoBrowserRelayStore:
    """
    按照数据库部署类型创建 ego-browser relay 票据存储。

    :param settings (Settings): 应用配置

    :return EgoBrowserRelayStore: 按当前部署模式创建的短期状态存储
    """

    if settings.database_url.startswith("sqlite"):
        return InMemoryEgoBrowserRelayStore()
    return RedisEgoBrowserRelayStore(Redis.from_url(settings.redis_url, decode_responses=True))
