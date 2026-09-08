from typing import cast
from uuid import UUID, uuid4

from redis.asyncio import Redis

from agent_remote_server.device_control.relay_store import DeviceRelayBinding, RedisDeviceRelayStore
from agent_remote_server.ego_browser.relay import EgoBrowserRelayBinding, EgoBrowserRelayHub
from agent_remote_server.relay.binding import RelayBinding


def test_relay_binding_kind_separates_identical_business_identifiers() -> None:
    """相同 ID 和 generation 不会让两类 relay binding 发生碰撞。"""

    binding_id = uuid4()
    device_binding = DeviceRelayBinding(
        user_id=uuid4(),
        device_id=uuid4(),
        tool_session_id=uuid4(),
        device_session_id=binding_id,
        node_id=uuid4(),
        generation=7,
    )
    browser_binding = EgoBrowserRelayBinding(
        user_id=device_binding.user_id,
        ego_browser_device_id=device_binding.device_id,
        tool_session_id=device_binding.tool_session_id,
        binding_id=binding_id,
        node_id=device_binding.node_id,
        generation=7,
    )

    assert device_binding.relay_binding != browser_binding.relay_binding
    assert len({device_binding.relay_binding, browser_binding.relay_binding}) == 2


def test_generic_relay_identity_preserves_deployed_redis_namespaces() -> None:
    """通用 relay 身份不会改变旧设备或浏览器 Redis 键格式。"""

    binding_id = uuid4()
    device_binding = DeviceRelayBinding(
        user_id=uuid4(),
        device_id=uuid4(),
        tool_session_id=uuid4(),
        device_session_id=binding_id,
        node_id=uuid4(),
        generation=9,
    )
    device_store = RedisDeviceRelayStore(cast(Redis, object()))

    assert device_store._exchange_key(device_binding) == (
        f"agent-remote:device-relay-exchange:{binding_id}:9"
    )
    assert EgoBrowserRelayHub._presence_key(browser_key := _browser_key(binding_id), "bridge") == (
        f"agent-remote:ego-browser:relay-presence:{binding_id}:9:bridge"
    )
    assert browser_key != device_binding.relay_binding


def _browser_key(binding_id: UUID) -> RelayBinding:
    return RelayBinding(kind="ego_browser", binding_id=binding_id, generation=9)
