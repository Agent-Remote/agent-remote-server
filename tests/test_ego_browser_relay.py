import asyncio
import base64
import json
import logging
import os
from typing import cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from redis.asyncio import Redis

from agent_remote_server.ego_browser.relay import (
    EgoBrowserProofChallengeClaims,
    EgoBrowserRelayBinding,
    EgoBrowserRelayHub,
    EgoBrowserRelayRole,
    EgoBrowserRelayTicketClaims,
    EgoBrowserRevocationBus,
    InMemoryEgoBrowserRelayStore,
    parse_outer_envelope,
)
from agent_remote_server.logging import JsonFormatter


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _outer(
    *,
    direction: str = "request",
    message_type: str | None = None,
    key_wrap: str | None = None,
    binding_id: str = "binding-1",
) -> dict[str, object]:
    """创建测试用的严格 outer envelope。"""

    return {
        "protocol": "ego-browser-bridge-v1",
        "channel": "ego_browser_bridge",
        "relay_binding_kind": "ego_browser",
        "type": message_type or ("execute" if direction == "request" else "execute_result"),
        "request_id": "request-1",
        "binding_id": binding_id,
        "generation": 1,
        "sequence": 1,
        "direction": direction,
        "payload_bytes": 1,
        "nonce": _encoded(b"n" * 12),
        "ciphertext": _encoded(b"c"),
        "auth_tag": _encoded(b"t" * 16),
        "key_wrap": key_wrap
        if key_wrap is not None
        else _encoded(b"k" * 92)
        if direction == "request" and message_type != "cancel"
        else "",
    }


def _raw(value: dict[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def test_proof_challenge_is_consumed_once_under_concurrency() -> None:
    """并发 worker 只能有一个成功消费同一 PoP challenge。"""

    async def scenario() -> None:
        store = InMemoryEgoBrowserRelayStore()
        claims = EgoBrowserProofChallengeClaims(
            user_id=uuid4(),
            ego_browser_device_id=uuid4(),
            operation="renew_binding",
            generation=7,
            binding_id=uuid4(),
        )
        await store.issue_proof_challenge(token_hash="proof-hash", claims=claims, ttl=60)
        consumed = await asyncio.gather(
            store.consume_proof_challenge(token_hash="proof-hash"),
            store.consume_proof_challenge(token_hash="proof-hash"),
        )
        assert consumed.count(claims) == 1
        assert consumed.count(None) == 1

    asyncio.run(scenario())


def test_parse_outer_envelope_accepts_canonical_request() -> None:
    """验证合法请求 outer envelope 可以通过严格校验。"""

    value = _outer()
    assert parse_outer_envelope(_raw(value), maximum_bytes=16_384) == value


def test_parse_outer_envelope_accepts_request_cancellation_without_key_wrap() -> None:
    """request cancellation 复用原身份和密钥，不携带新的 key wrap。"""

    value = _outer(message_type="cancel", key_wrap="")
    assert parse_outer_envelope(_raw(value), maximum_bytes=16_384) == value

    value["key_wrap"] = _encoded(b"k" * 92)
    with pytest.raises(ValueError, match="key_wrap"):
        parse_outer_envelope(_raw(value), maximum_bytes=16_384)


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda value: value.pop("key_wrap"), "outer_fields"),
        (lambda value: value.update(key_wrap="not-base64!!!"), "key_wrap"),
        (lambda value: value.update(key_wrap=_encoded(b"k" * 91)), "key_wrap"),
    ],
)
def test_parse_outer_envelope_rejects_invalid_request_key_wrap(
    mutator: object, expected: str
) -> None:
    """验证请求缺少或篡改 key wrap 时被拒绝。"""

    value = _outer()
    mutator(value)  # type: ignore[operator]
    with pytest.raises(ValueError, match=expected):
        parse_outer_envelope(_raw(value), maximum_bytes=16_384)


def test_parse_outer_envelope_rejects_response_key_wrap() -> None:
    """验证 response outer envelope 不得携带 key wrap。"""

    value = _outer(direction="response", key_wrap=_encoded(b"k" * 92))
    with pytest.raises(ValueError, match="key_wrap"):
        parse_outer_envelope(_raw(value), maximum_bytes=16_384)


def test_parse_outer_envelope_rejects_duplicate_json_keys() -> None:
    """验证重复 JSON 字段不会被解析器静默覆盖。"""

    raw = b'{"protocol":"ego-browser-bridge-v1","protocol":"ego-browser-bridge-v1"}'
    with pytest.raises(ValueError, match="invalid_outer_json"):
        parse_outer_envelope(raw, maximum_bytes=16_384)


def test_parse_outer_envelope_rejects_empty_ciphertext() -> None:
    """验证空密文不能伪装成零字节执行请求。"""

    value = _outer()
    value["payload_bytes"] = 0
    value["ciphertext"] = ""
    with pytest.raises(ValueError, match="payload_bytes"):
        parse_outer_envelope(_raw(value), maximum_bytes=16_384)


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
        self.messages.put_nowait({"type": "websocket.disconnect", "code": code})


def _binding() -> EgoBrowserRelayBinding:
    return EgoBrowserRelayBinding(
        user_id=uuid4(),
        ego_browser_device_id=uuid4(),
        tool_session_id=uuid4(),
        binding_id=uuid4(),
        node_id=uuid4(),
        generation=1,
    )


def _claims(
    binding: EgoBrowserRelayBinding,
    role: EgoBrowserRelayRole,
) -> EgoBrowserRelayTicketClaims:
    return EgoBrowserRelayTicketClaims(binding=binding, role=role)


def test_redis_relay_hubs_pair_and_forward_across_workers() -> None:
    """两个独立 worker 通过 Redis 配对并转发 opaque frame。"""

    redis_url = os.getenv("AGENT_REMOTE_INTEGRATION_REDIS_URL")
    if redis_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_REDIS_URL is not configured")

    async def scenario() -> None:
        bridge_redis = Redis.from_url(redis_url, decode_responses=False)
        wrapper_redis = Redis.from_url(redis_url, decode_responses=False)
        bridge_hub = _distributed_hub(bridge_redis)
        wrapper_hub = _distributed_hub(wrapper_redis)
        bridge = _FakeWebSocket()
        wrapper = _FakeWebSocket()
        binding = _binding()
        frame = _raw(_outer(binding_id=str(binding.binding_id)))
        wrapper.messages.put_nowait({"type": "websocket.receive", "bytes": frame})
        wrapper.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})
        validated: list[tuple[EgoBrowserRelayRole, bytes]] = []

        async def validate(
            claims: EgoBrowserRelayTicketClaims,
            raw: bytes,
            _envelope: dict[str, object],
        ) -> None:
            validated.append((claims.role, raw))

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    bridge_hub.connect(
                        _claims(binding, "bridge"), cast(WebSocket, bridge), validate
                    ),
                    wrapper_hub.connect(
                        _claims(binding, "wrapper"), cast(WebSocket, wrapper), validate
                    ),
                ),
                timeout=3,
            )
            assert bridge.accepted and wrapper.accepted
            assert bridge.sent == [frame]
            assert validated == [("wrapper", frame)]
            assert 1011 in bridge.close_codes
        finally:
            await asyncio.gather(bridge_hub.close(), wrapper_hub.close())

    asyncio.run(scenario())


def test_redis_relay_hubs_reject_duplicate_role_across_workers() -> None:
    """Redis presence key 在不同 worker 间原子拒绝重复角色。"""

    redis_url = os.getenv("AGENT_REMOTE_INTEGRATION_REDIS_URL")
    if redis_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_REDIS_URL is not configured")

    async def scenario() -> None:
        first_hub = _distributed_hub(Redis.from_url(redis_url, decode_responses=False))
        second_hub = _distributed_hub(Redis.from_url(redis_url, decode_responses=False))
        first = _FakeWebSocket()
        duplicate = _FakeWebSocket()
        binding = _binding()

        async def validate(
            _claims: EgoBrowserRelayTicketClaims,
            _raw: bytes,
            _envelope: dict[str, object],
        ) -> None:
            return None

        first_task = asyncio.create_task(
            first_hub.connect(_claims(binding, "wrapper"), cast(WebSocket, first), validate)
        )
        try:
            for _ in range(100):
                if first.accepted:
                    await asyncio.sleep(0.05)
                    break
                await asyncio.sleep(0.01)
            await second_hub.connect(
                _claims(binding, "wrapper"), cast(WebSocket, duplicate), validate
            )
            assert duplicate.accepted
            assert 1008 in duplicate.close_codes
            await first_hub.close_binding(binding.binding_id, binding.generation, publish=False)
            await asyncio.wait_for(first_task, timeout=1)
        finally:
            first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
            await asyncio.gather(first_hub.close(), second_hub.close())

    asyncio.run(scenario())


def test_redis_revocation_marker_closes_and_rejects_without_pubsub() -> None:
    """共享撤销标记在 worker 漏收 Pub/Sub 时仍关闭并拒绝旧 generation。"""

    redis_url = os.getenv("AGENT_REMOTE_INTEGRATION_REDIS_URL")
    if redis_url is None:
        pytest.skip("AGENT_REMOTE_INTEGRATION_REDIS_URL is not configured")

    async def scenario() -> None:
        binding = _binding()
        bridge_hub = _distributed_hub(Redis.from_url(redis_url, decode_responses=False))
        wrapper_hub = _distributed_hub(Redis.from_url(redis_url, decode_responses=False))
        retry_hub = _distributed_hub(Redis.from_url(redis_url, decode_responses=False))
        probe = Redis.from_url(redis_url, decode_responses=False)
        publisher = EgoBrowserRevocationBus(
            Redis.from_url(redis_url, decode_responses=True),
            Redis.from_url(redis_url, decode_responses=True),
            channel=f"agent-remote:test:unused-revocation:{binding.binding_id}",
        )
        bridge = _FakeWebSocket()
        wrapper = _FakeWebSocket()
        retry = _FakeWebSocket()
        presence_keys = [
            (
                "agent-remote:ego-browser:relay-presence:"
                f"{binding.binding_id}:{binding.generation}:{role}"
            )
            for role in ("bridge", "wrapper")
        ]
        revocation_key = (
            f"agent-remote:ego-browser:revoked:{binding.binding_id}:{binding.generation}"
        )

        async def validate(
            _claims: EgoBrowserRelayTicketClaims,
            _raw: bytes,
            _envelope: dict[str, object],
        ) -> None:
            return None

        tasks = [
            asyncio.create_task(
                bridge_hub.connect(_claims(binding, "bridge"), cast(WebSocket, bridge), validate)
            ),
            asyncio.create_task(
                wrapper_hub.connect(_claims(binding, "wrapper"), cast(WebSocket, wrapper), validate)
            ),
        ]
        try:
            for _ in range(100):
                if await probe.exists(*presence_keys) == 2:
                    break
                await asyncio.sleep(0.01)
            assert await probe.exists(*presence_keys) == 2

            # 此处刻意不启动 publisher，用于验证没有 worker 收到 Pub/Sub 时的拒绝行为。
            await publisher.publish(binding.binding_id, binding.generation)
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
            assert 1008 in bridge.close_codes
            assert 1008 in wrapper.close_codes

            await asyncio.wait_for(
                retry_hub.connect(_claims(binding, "wrapper"), cast(WebSocket, retry), validate),
                timeout=1,
            )
            assert retry.accepted
            assert 1008 in retry.close_codes
            assert await probe.exists(*presence_keys) == 0
            assert 1_100 <= await probe.ttl(revocation_key) <= 1_200
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await probe.delete(revocation_key, *presence_keys)
            await asyncio.gather(
                bridge_hub.close(),
                wrapper_hub.close(),
                retry_hub.close(),
                publisher.close(),
                probe.aclose(),
            )

    asyncio.run(scenario())


def test_relay_metrics_are_content_free(caplog: pytest.LogCaptureFixture) -> None:
    """relay 指标只记录固定维度、计数和密文字节数。"""

    async def scenario() -> tuple[int, str]:
        hub = EgoBrowserRelayHub(
            maximum_frame_bytes=16_384,
            pair_timeout_seconds=1,
            maximum_bytes_per_second=32_768,
            maximum_connection_seconds=1,
        )
        bridge = _FakeWebSocket()
        wrapper = _FakeWebSocket()
        binding = _binding()
        frame = _raw(_outer(binding_id=str(binding.binding_id)))
        wrapper.messages.put_nowait({"type": "websocket.receive", "bytes": frame})
        wrapper.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})

        async def validate(
            _claims: EgoBrowserRelayTicketClaims,
            _raw: bytes,
            _envelope: dict[str, object],
        ) -> None:
            return None

        await asyncio.gather(
            hub.connect(_claims(binding, "bridge"), cast(WebSocket, bridge), validate),
            hub.connect(_claims(binding, "wrapper"), cast(WebSocket, wrapper), validate),
        )
        await hub.close()
        return len(frame), str(binding.binding_id)

    with caplog.at_level(logging.INFO, logger="agent_remote_server.ego_browser.relay"):
        expected_bytes, binding_text = asyncio.run(scenario())

    metrics = [
        record
        for record in caplog.records
        if getattr(record, "metric_name", "").startswith("ego_browser_")
    ]
    assert len(metrics) == 6
    connection_metrics = [
        record
        for record in metrics
        if getattr(record, "metric_name", "") == "ego_browser_bridge_connections"
    ]
    assert {
        (getattr(record, "relay_role", ""), getattr(record, "metric_operation", ""))
        for record in connection_metrics
    } == {
        ("bridge", "opened"),
        ("bridge", "closed"),
        ("wrapper", "opened"),
        ("wrapper", "closed"),
    }
    assert (
        sum(
            int(getattr(record, "metric_value", 0))
            for record in connection_metrics
            if getattr(record, "metric_operation", "") == "closed"
        )
        == 0
    )
    byte_metrics = [
        record
        for record in metrics
        if getattr(record, "metric_name", "") == "ego_browser_bytes_total"
    ]
    assert {getattr(record, "metric_direction", "") for record in byte_metrics} == {
        "request",
        "response",
    }
    assert sum(int(getattr(record, "frame_count", 0)) for record in byte_metrics) == 1
    assert sum(int(getattr(record, "metric_value", 0)) for record in byte_metrics) == expected_bytes
    rendered = "\n".join(record.getMessage() for record in metrics)
    assert binding_text not in rendered
    assert "request-1" not in rendered
    structured = json.loads(JsonFormatter().format(byte_metrics[0]))
    assert structured["metric_name"] == "ego_browser_bytes_total"
    assert structured["metric_unit"] == "bytes"
    assert "binding_id" not in structured


def _distributed_hub(redis: Redis) -> EgoBrowserRelayHub:
    return EgoBrowserRelayHub(
        maximum_frame_bytes=16_384,
        pair_timeout_seconds=1,
        maximum_bytes_per_second=32_768,
        maximum_connection_seconds=2,
        redis=redis,
    )
