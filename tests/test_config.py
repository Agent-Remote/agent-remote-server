"""
验证配置行为。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_remote_server.config import Settings, canonicalize_origin
from agent_remote_server.ego_browser.release_policy import (
    EGO_BROWSER_LOCAL_RUNTIME_VERSION,
    EGO_BROWSER_PROTOCOL_VERSION,
    EGO_BROWSER_SKILL_VERSION,
    EGO_BROWSER_WRAPPER_VERSION,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://Example.COM:443/", "https://example.com"),
        ("http://localhost:80/", "http://localhost"),
        ("https://[0:0:0:0:0:0:0:1]:443", "https://[::1]"),
        ("https://bücher.example", "https://xn--bcher-kva.example"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ],
)
def test_canonicalize_origin_returns_one_stable_origin(value: str, expected: str) -> None:
    """
    规范化大小写、默认端口、IPv6 和 IDNA 表示。

    :param value (str): 值
    :param expected (str): 预期值
    """

    assert canonicalize_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:",
        "https://host:",
        "https://[::1]foo",
        "https://::1",
        "https://[fe80::1%25en0]",
        "https://foo_bar.example",
        "https://a..b.example",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?query",
        "https://example.com#fragment",
        "https://example.com\\other",
        "http://192.0.2.10:8080",
    ],
)
def test_canonicalize_origin_rejects_ambiguous_or_unsafe_values(value: str) -> None:
    """
    不明确 authority、内容组件和生产 HTTP 地址必须拒绝。

    :param value (str): 值
    """

    with pytest.raises(ValueError):
        canonicalize_origin(value)


def test_explicit_production_loopback_http_is_not_a_legacy_default() -> None:
    """
    生产显式配置 localhost HTTP 不得借用历史测试例外。
    """

    with pytest.raises(ValueError):
        Settings(environment="production", public_base_url="http://localhost:8000")


def test_settings_use_python_313_project_defaults() -> None:
    """
    验证 Python 3.13 项目默认配置。
    """
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
    assert settings.ego_browser_bridge_enabled is False
    assert settings.ego_browser_expected_wrapper_version == EGO_BROWSER_WRAPPER_VERSION
    assert settings.ego_browser_expected_skill_version == EGO_BROWSER_SKILL_VERSION
    assert settings.ego_browser_expected_local_runtime_version == EGO_BROWSER_LOCAL_RUNTIME_VERSION
    assert settings.ego_browser_expected_protocol_version == EGO_BROWSER_PROTOCOL_VERSION
    assert settings.ego_browser_expected_learning_bundle_digest == ""
    assert settings.ego_browser_expected_distribution_version == ""
    assert settings.ego_browser_expected_root_manifest_sha256 == ""
    assert settings.ego_browser_expected_bridge_release_manifest_sha256 == ""


def test_device_control_v2_can_be_disabled_for_emergency_rollback() -> None:
    """
    v2 默认启用，同时保留显式紧急关闭开关。
    """

    assert Settings(device_control_v2_enabled=False).device_control_v2_enabled is False


def test_device_session_authorization_mode_is_closed_to_known_values() -> None:
    """
    设备会话授权模式只接受显式的兼容或全信任值。
    """

    assert (
        Settings(
            device_session_authorization_mode="session_full_trust"
        ).device_session_authorization_mode
        == "session_full_trust"
    )
    with pytest.raises(ValidationError):
        Settings(device_session_authorization_mode="unbounded")  # type: ignore[arg-type]


def test_example_environment_uses_the_default_v2_switch() -> None:
    """
    示例部署不重复中央发布策略，也不保留旧灰度和验收窗口。
    """

    example = Path(".env.example").read_text(encoding="utf-8")
    assert "DEVICE_CONTROL_V2_ENABLED=true" in example
    assert "EGO_BROWSER_BRIDGE_ENABLED=false" in example
    for field in (
        "EGO_BROWSER_EXPECTED_WRAPPER_VERSION",
        "EGO_BROWSER_EXPECTED_SKILL_VERSION",
        "EGO_BROWSER_EXPECTED_SKILL_TREE_SHA256",
        "EGO_BROWSER_EXPECTED_SKILL_COMMIT",
        "EGO_BROWSER_EXPECTED_LOCAL_RUNTIME_VERSION",
        "EGO_BROWSER_EXPECTED_PROTOCOL_VERSION",
        "EGO_BROWSER_EXPECTED_LEARNING_BUNDLE_SIGNING_KEY_ID",
    ):
        assert f"{field}=" not in example
    assert "DEVICE_CONTROL_V2_ROLLOUT_PERCENT" not in example
    assert "DEVICE_CONTROL_V2_ACCEPTANCE" not in example


def test_port_forward_settings_reject_incoherent_ranges() -> None:
    """
    端口和 TTL 范围必须保持有序。
    """

    with pytest.raises(ValidationError):
        Settings(port_forward_min_port=6000, port_forward_max_port=5000)
    with pytest.raises(ValidationError):
        Settings(port_forward_default_ttl_seconds=3600, port_forward_max_ttl_seconds=60)


def test_device_relay_settings_reject_an_incoherent_rate_limit() -> None:
    """
    设备中继每秒速率上限不得小于单帧上限。
    """

    with pytest.raises(ValidationError, match="byte rate"):
        Settings(
            device_relay_max_frame_bytes=1_048_576,
            device_relay_max_bytes_per_second=524_288,
        )


def test_production_device_control_requires_explicit_coherent_retention() -> None:
    """
    生产设备控制必须显式配置会话和审计保留期限。
    """

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


def test_production_ego_browser_settings_are_validated_at_startup() -> None:
    """
    配置阶段允许表达目标策略，启动阶段仍必须提供签名发布证据。
    """

    settings = Settings(
        environment="production",
        ego_browser_bridge_enabled=True,
        ego_browser_require_device_pop=True,
        ego_browser_expected_release_profile="community-local-trust",
        ego_browser_expected_signer_certificate_sha256="a" * 64,
    )

    assert settings.ego_browser_bridge_enabled is True
