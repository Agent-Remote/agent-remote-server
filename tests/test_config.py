import pytest
from pydantic import ValidationError

from agent_remote_server.config import Settings


def test_settings_use_python_313_project_defaults() -> None:
    settings = Settings(secret_key="test-secret")

    assert settings.app_name == "agent-remote-server"
    assert settings.environment == "development"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url.startswith("redis://")
    assert settings.access_token_ttl_seconds == 3600
    assert settings.device_token_ttl_seconds == 2_592_000


def test_port_forward_settings_reject_incoherent_ranges() -> None:
    """端口和 TTL 范围必须保持有序。"""

    with pytest.raises(ValidationError):
        Settings(port_forward_min_port=6000, port_forward_max_port=5000)
    with pytest.raises(ValidationError):
        Settings(port_forward_default_ttl_seconds=3600, port_forward_max_ttl_seconds=60)
