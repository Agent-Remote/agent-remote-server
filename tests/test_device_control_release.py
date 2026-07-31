import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_remote_server import __version__
from agent_remote_server.config import Settings
from agent_remote_server.device_control_release import (
    DeviceControlReleaseEvidence,
    DeviceControlReleaseEvidenceError,
    ensure_device_control_release_evidence_current,
    verify_device_control_release_evidence,
)
from agent_remote_server.main import create_app

_DIGEST = "a" * 64


def create_signed_evidence(
    path: Path,
    *,
    expires_at: datetime,
    issued_at: datetime | None = None,
    release_version: str = __version__,
) -> str:
    """创建使用临时密钥签名的测试发布证据。"""

    private_key = Ed25519PrivateKey.generate()
    manifest = DeviceControlReleaseEvidence(
        schema_version=1,
        release_version=release_version,
        issued_at=issued_at or expires_at - timedelta(days=1),
        expires_at=expires_at,
        server_sha256=_DIGEST,
        node_sha256=_DIGEST,
        application_sha256=_DIGEST,
        proxy_sha256=_DIGEST,
        sbom_sha256=_DIGEST,
        provenance_sha256=_DIGEST,
        security_tests_sha256=_DIGEST,
        security_review_sha256=_DIGEST,
        signing_notarization_sha256=_DIGEST,
        outbound_policy_sha256=_DIGEST,
        local_claude_isolation_sha256=_DIGEST,
        stop_revocation_sha256=_DIGEST,
        compatibility_sha256=_DIGEST,
        ci_run_url="https://ci.example.test/runs/123",
        signature="pending",
    )
    signature = private_key.sign(manifest.signing_payload())
    signed_manifest = manifest.model_copy(
        update={"signature": base64.b64encode(signature).decode("ascii")}
    )
    path.write_text(
        json.dumps(signed_manifest.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public_key).decode("ascii")


def test_release_evidence_accepts_valid_signed_manifest(tmp_path: Path) -> None:
    """有效签名、版本和有效期应允许生产发布。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(evidence_path, expires_at=now + timedelta(days=1))

    evidence = verify_device_control_release_evidence(
        evidence_path=str(evidence_path),
        public_key_base64=public_key,
        now=now,
    )

    assert evidence.release_version == __version__


def test_release_evidence_rejects_tampering(tmp_path: Path) -> None:
    """签名后的任何证据字段变更都必须拒绝。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(evidence_path, expires_at=now + timedelta(days=1))
    content = json.loads(evidence_path.read_text(encoding="utf-8"))
    content["outbound_policy_sha256"] = "b" * 64
    evidence_path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(DeviceControlReleaseEvidenceError, match="signature is invalid"):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_path),
            public_key_base64=public_key,
            now=now,
        )


def test_release_evidence_rejects_symlinks_and_duplicate_fields(tmp_path: Path) -> None:
    """生产证据必须拒绝符号链接和任何层级的重复 JSON 字段。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(evidence_path, expires_at=now + timedelta(days=1))
    evidence_link = tmp_path / "release-evidence-link.json"
    evidence_link.symlink_to(evidence_path)

    with pytest.raises(DeviceControlReleaseEvidenceError, match="could not be loaded"):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_link),
            public_key_base64=public_key,
            now=now,
        )

    content = evidence_path.read_text(encoding="utf-8")
    evidence_path.write_text(
        content.replace(
            '"schema_version": 1',
            '"schema_version": 1, "schema_version": 1',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(DeviceControlReleaseEvidenceError, match="could not be loaded"):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_path),
            public_key_base64=public_key,
            now=now,
        )


def test_release_evidence_rejects_empty_and_oversized_files(tmp_path: Path) -> None:
    """空清单和超过固定读取上限的清单必须在解析前拒绝。"""

    evidence_path = tmp_path / "release-evidence.json"
    public_key = base64.b64encode(b"a" * 32).decode("ascii")

    evidence_path.write_bytes(b"")
    with pytest.raises(DeviceControlReleaseEvidenceError, match="could not be loaded"):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_path),
            public_key_base64=public_key,
        )

    evidence_path.write_bytes(b"{" + b" " * 65_536 + b"}")
    with pytest.raises(DeviceControlReleaseEvidenceError, match="could not be loaded"):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_path),
            public_key_base64=public_key,
        )


@pytest.mark.parametrize(
    ("expires_at", "release_version", "message"),
    [
        (datetime(2026, 7, 31, tzinfo=UTC), __version__, "has expired"),
        (datetime(2026, 8, 1, tzinfo=UTC), "999.0.0", "does not match"),
    ],
)
def test_release_evidence_rejects_expiry_and_version_mismatch(
    tmp_path: Path,
    expires_at: datetime,
    release_version: str,
    message: str,
) -> None:
    """过期证据或其他服务端版本的证据必须拒绝。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=expires_at,
        release_version=release_version,
    )

    with pytest.raises(DeviceControlReleaseEvidenceError, match=message):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_path),
            public_key_base64=public_key,
            now=now,
        )


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "message"),
    [
        (
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 2, tzinfo=UTC),
            "not yet valid",
        ),
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 8, 1, tzinfo=UTC),
            "lifetime is invalid",
        ),
    ],
)
def test_release_evidence_rejects_future_issue_and_excessive_lifetime(
    tmp_path: Path,
    issued_at: datetime,
    expires_at: datetime,
    message: str,
) -> None:
    """未来签发或超过最长生命周期的证据必须拒绝。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        issued_at=issued_at,
        expires_at=expires_at,
    )

    with pytest.raises(DeviceControlReleaseEvidenceError, match=message):
        verify_device_control_release_evidence(
            evidence_path=str(evidence_path),
            public_key_base64=public_key,
            now=now,
        )


def test_production_device_control_requires_release_evidence() -> None:
    """生产环境不能只通过功能开关启用设备控制。"""

    settings = Settings(
        secret_key="test-secret",
        environment="production",
        device_control_enabled=True,
        device_session_retention_days=30,
        device_session_audit_retention_days=90,
    )

    with pytest.raises(DeviceControlReleaseEvidenceError, match="requires release evidence"):
        create_app(settings)


def test_production_device_control_accepts_valid_release_evidence(tmp_path: Path) -> None:
    """生产环境应接受当前版本的有效签名发布证据。"""

    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    app = create_app(
        Settings(
            secret_key="test-secret",
            environment="production",
            device_control_enabled=True,
            device_session_retention_days=30,
            device_session_audit_retention_days=90,
            device_control_release_evidence_path=str(evidence_path),
            device_control_release_public_key=public_key,
        )
    )

    assert app.state.settings.device_control_enabled is True


def test_development_device_control_does_not_require_release_evidence() -> None:
    """开发环境仍可使用无敏感数据的显式设备控制测试。"""

    app = create_app(
        Settings(
            secret_key="test-secret",
            environment="development",
            device_control_enabled=True,
        )
    )

    assert app.state.settings.device_control_enabled is True


def test_runtime_release_gate_rejects_expired_production_evidence() -> None:
    """长时间运行的生产服务不得在证据过期后继续推进设备控制。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence = DeviceControlReleaseEvidence(
        schema_version=1,
        release_version=__version__,
        issued_at=now - timedelta(days=2),
        expires_at=now - timedelta(seconds=1),
        server_sha256=_DIGEST,
        node_sha256=_DIGEST,
        application_sha256=_DIGEST,
        proxy_sha256=_DIGEST,
        sbom_sha256=_DIGEST,
        provenance_sha256=_DIGEST,
        security_tests_sha256=_DIGEST,
        security_review_sha256=_DIGEST,
        signing_notarization_sha256=_DIGEST,
        outbound_policy_sha256=_DIGEST,
        local_claude_isolation_sha256=_DIGEST,
        stop_revocation_sha256=_DIGEST,
        compatibility_sha256=_DIGEST,
        ci_run_url="https://ci.example.test/runs/expired",
        signature="test-only",
    )

    with pytest.raises(DeviceControlReleaseEvidenceError, match="has expired"):
        ensure_device_control_release_evidence_current(
            environment="production",
            enabled=True,
            evidence=evidence,
            now=now,
        )

    ensure_device_control_release_evidence_current(
        environment="development",
        enabled=True,
        evidence=None,
        now=now,
    )
