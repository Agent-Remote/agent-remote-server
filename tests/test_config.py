from pathlib import Path

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
    assert settings.device_control_enabled is False
    assert settings.device_control_v2_enabled is True
    assert settings.device_session_authorization_mode == "per_application_approval"
    assert settings.device_control_release_evidence_path == ""
    assert settings.device_control_release_public_key == ""
    assert settings.device_session_retention_days == 0
    assert settings.device_session_audit_retention_days == 0
    assert settings.device_relay_max_bytes_per_second == 8_388_608
    assert settings.device_relay_max_connection_seconds == 900


def test_device_control_v2_can_be_disabled_for_emergency_rollback() -> None:
    """v2 默认启用，同时保留显式紧急关闭开关。"""

    assert Settings(device_control_v2_enabled=False).device_control_v2_enabled is False


def test_device_session_authorization_mode_is_closed_to_known_values() -> None:
    """设备会话授权模式只接受显式的兼容或全信任值。"""

    assert (
        Settings(
            device_session_authorization_mode="session_full_trust"
        ).device_session_authorization_mode
        == "session_full_trust"
    )
    with pytest.raises(ValidationError):
        Settings(device_session_authorization_mode="unbounded")  # type: ignore[arg-type]


def test_example_environment_uses_the_default_v2_switch() -> None:
    """示例部署只公开默认开启的 v2 开关，不保留旧灰度和验收窗口。"""

    example = Path(".env.example").read_text(encoding="utf-8")
    assert "DEVICE_CONTROL_V2_ENABLED=true" in example
    assert "DEVICE_CONTROL_V2_ROLLOUT_PERCENT" not in example
    assert "DEVICE_CONTROL_V2_ACCEPTANCE" not in example


def test_port_forward_settings_reject_incoherent_ranges() -> None:
    """端口和 TTL 范围必须保持有序。"""

    with pytest.raises(ValidationError):
        Settings(port_forward_min_port=6000, port_forward_max_port=5000)
    with pytest.raises(ValidationError):
        Settings(port_forward_default_ttl_seconds=3600, port_forward_max_ttl_seconds=60)


def test_device_relay_settings_reject_an_incoherent_rate_limit() -> None:
    """设备中继每秒速率上限不得小于单帧上限。"""

    with pytest.raises(ValidationError, match="byte rate"):
        Settings(
            device_relay_max_frame_bytes=1_048_576,
            device_relay_max_bytes_per_second=524_288,
        )


def test_production_device_control_requires_explicit_coherent_retention() -> None:
    """生产设备控制必须显式配置会话和审计保留期限。"""

    with pytest.raises(ValidationError, match="explicit metadata retention"):
        Settings(environment="production", device_control_enabled=True)
    with pytest.raises(ValidationError, match="audit retention"):
        Settings(device_session_retention_days=30, device_session_audit_retention_days=10)

    settings = Settings(
        environment="production",
        device_control_enabled=True,
        device_session_retention_days=30,
        device_session_audit_retention_days=90,
    )
    assert settings.device_session_retention_days == 30
    assert settings.device_session_audit_retention_days == 90
