from datetime import UTC, datetime, timedelta
from uuid import uuid4

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
    assert settings.device_control_v2_rollout_percent == 0
    assert settings.device_control_v2_acceptance_device_id is None
    assert settings.device_control_v2_acceptance_expires_at is None
    assert settings.device_control_release_evidence_path == ""
    assert settings.device_control_release_public_key == ""
    assert settings.device_session_retention_days == 0
    assert settings.device_session_audit_retention_days == 0
    assert settings.device_relay_max_bytes_per_second == 8_388_608
    assert settings.device_relay_max_connection_seconds == 900


def test_device_control_v2_rollout_percentage_is_bounded() -> None:
    """v2 灰度比例只能位于闭区间 0 到 100。"""

    assert Settings(device_control_v2_rollout_percent=100).device_control_v2_rollout_percent == 100
    with pytest.raises(ValidationError):
        Settings(device_control_v2_rollout_percent=-1)
    with pytest.raises(ValidationError):
        Settings(device_control_v2_rollout_percent=101)


def test_device_control_v2_acceptance_settings_are_bounded() -> None:
    """v2 验收只允许生产环境中的单设备零灰度配置。"""

    expires_at = datetime.now(UTC) + timedelta(hours=1)
    device_id = uuid4()
    settings = Settings(
        environment="production",
        device_control_enabled=True,
        device_session_retention_days=30,
        device_session_audit_retention_days=90,
        device_control_v2_acceptance_device_id=device_id,
        device_control_v2_acceptance_expires_at=expires_at,
    )
    assert settings.device_control_v2_acceptance_device_id == device_id

    with pytest.raises(ValidationError, match="configured together"):
        Settings(
            environment="production",
            device_control_enabled=True,
            device_session_retention_days=30,
            device_session_audit_retention_days=90,
            device_control_v2_acceptance_device_id=device_id,
        )
    with pytest.raises(ValidationError, match="zero global rollout"):
        Settings(
            environment="production",
            device_control_enabled=True,
            device_session_retention_days=30,
            device_session_audit_retention_days=90,
            device_control_v2_rollout_percent=1,
            device_control_v2_acceptance_device_id=device_id,
            device_control_v2_acceptance_expires_at=expires_at,
        )


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
