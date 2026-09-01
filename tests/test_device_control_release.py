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
    ReleaseComponentIdentity,
    ensure_device_control_release_evidence_current,
    verify_device_control_release_evidence,
)
from agent_remote_server.main import create_app

_DIGEST = "a" * 64


def create_signed_evidence(
    path: Path,
    *,
    expires_at: datetime | None = None,
    issued_at: datetime | None = None,
    release_version: str = __version__,
    schema_version: int = 2,
    apple_profile: bool = False,
    computer_use_v2_evidence_sha256: str | None = None,
) -> str:
    """创建使用临时密钥签名的测试发布证据。"""

    private_key = Ed25519PrivateKey.generate()
    target_digests = {
        "linux-amd64-glibc": "a" * 64,
        "linux-arm64-glibc": "b" * 64,
        "linux-amd64-musl": "c" * 64,
        "linux-arm64-musl": "d" * 64,
    }
    release_artifacts = (
        {
            "node_artifacts_sha256": target_digests,
            "proxy_artifacts_sha256": target_digests,
        }
        if schema_version in {3, 4, 5, 6, 8} and not apple_profile
        else {"node_sha256": _DIGEST, "proxy_sha256": _DIGEST}
    )
    profile_fields: dict[str, object]
    if apple_profile:
        if schema_version not in {1, 7, 8}:
            raise ValueError("Apple test evidence requires an Apple schema")
        profile_fields = {
            "security_tests_sha256": _DIGEST,
            "security_review_sha256": _DIGEST,
            "outbound_policy_sha256": _DIGEST,
            "local_claude_isolation_sha256": _DIGEST,
            "stop_revocation_sha256": _DIGEST,
            "compatibility_sha256": _DIGEST,
            "computer_use_v2_evidence_sha256": computer_use_v2_evidence_sha256,
        }
    else:
        profile_fields = {
            "release_profile": "community-local-trust",
            "apple_notarized": False,
            "public_distribution": False,
            "manual_trust_required": True,
            "community_signing_sha256": _DIGEST,
            "automated_release_checks_sha256": _DIGEST,
            "risk_acceptance_sha256": _DIGEST,
            "computer_use_v2_evidence_sha256": computer_use_v2_evidence_sha256,
        }
    composition_fields = (
        {
            "distribution_version": "9.8.7",
            "release_manifest_sha256": "e" * 64,
            "components": {
                name: {
                    "repository": f"Agent-Remote/{name}",
                    "version": release_version if name == "agent-remote-server" else version,
                    "commit": character * 40,
                    "release_workflow": "release.yml",
                }
                for name, version, character in (
                    ("agent-remote-server", release_version, "1"),
                    ("agent-remote-node", "2.3.4", "2"),
                    ("agent-remote-cli", "3.4.5", "3"),
                    ("agent-remote-admin-web", "4.5.6", "4"),
                    ("agent-remote-device", "5.6.7", "5"),
                )
            },
        }
        if schema_version in {5, 6, 7, 8}
        else {}
    )
    if schema_version < 8 and expires_at is None:
        raise ValueError("legacy test evidence requires an expiry")
    effective_issued_at = issued_at or (
        expires_at - timedelta(days=1)
        if expires_at is not None
        else datetime(2026, 7, 30, tzinfo=UTC)
    )
    evidence_fields: dict[str, object] = {
        "schema_version": schema_version,
        "production_ready": True,
        "release_version": release_version,
        "issued_at": effective_issued_at,
        "server_sha256": _DIGEST,
        "application_sha256": _DIGEST,
        "sbom_sha256": _DIGEST,
        "provenance_sha256": _DIGEST,
        "signing_notarization_sha256": _DIGEST,
        "ci_run_url": "https://ci.example.test/runs/123",
        "signature": "pending",
        **release_artifacts,
        **profile_fields,
        **composition_fields,
    }
    if expires_at is not None:
        evidence_fields["expires_at"] = expires_at
    manifest = DeviceControlReleaseEvidence.model_validate(evidence_fields)
    signature = private_key.sign(manifest.signing_payload())
    signed_manifest = manifest.model_copy(
        update={"signature": base64.b64encode(signature).decode("ascii")}
    )
    path.write_text(
        json.dumps(
            signed_manifest.model_dump(mode="json", exclude_none=schema_version == 8),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public_key).decode("ascii")


def test_release_evidence_accepts_valid_signed_manifest(tmp_path: Path) -> None:
    """旧版有效签名、版本和有效期应允许迁移期生产发布。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(evidence_path, expires_at=now + timedelta(days=1))

    evidence = verify_device_control_release_evidence(
        evidence_path=str(evidence_path),
        public_key_base64=public_key,
        now=now,
    )

    assert evidence.release_version == __version__
    assert evidence.release_profile == "community-local-trust"


def test_release_evidence_schema_8_is_permanent_and_release_bound(tmp_path: Path) -> None:
    """schema 8 应与根发布组合绑定且不因验证时间推移而失效。"""

    issued_at = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        schema_version=8,
        issued_at=issued_at,
    )

    evidence = verify_device_control_release_evidence(
        evidence_path=str(evidence_path),
        public_key_base64=public_key,
        now=datetime(2036, 7, 31, tzinfo=UTC),
    )

    assert evidence.schema_version == 8
    assert evidence.distribution_version == "9.8.7"
    assert evidence.expires_at is None
    assert "expires_at" not in json.loads(evidence_path.read_text(encoding="utf-8"))
    ensure_device_control_release_evidence_current(
        environment="production",
        enabled=True,
        evidence=evidence,
        now=datetime(2036, 7, 31, tzinfo=UTC),
    )


def test_release_evidence_schema_8_rejects_expiry_field(tmp_path: Path) -> None:
    """永久 schema 不得重新引入 expires_at 字段。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    with pytest.raises(ValueError, match="must not contain expires_at"):
        create_signed_evidence(
            tmp_path / "release-evidence.json",
            schema_version=8,
            issued_at=now,
            expires_at=now + timedelta(days=1),
        )


def test_release_evidence_accepts_every_node_architecture(tmp_path: Path) -> None:
    """Schema 3 签名清单应同时绑定并验签全部 Node 与 Proxy 架构制品。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=now + timedelta(days=1),
        schema_version=3,
    )

    evidence = verify_device_control_release_evidence(
        evidence_path=str(evidence_path),
        public_key_base64=public_key,
        now=now,
    )

    expected_targets = {
        "linux-amd64-glibc",
        "linux-arm64-glibc",
        "linux-amd64-musl",
        "linux-arm64-musl",
    }
    assert set(evidence.node_artifacts_sha256 or {}) == expected_targets
    assert set(evidence.proxy_artifacts_sha256 or {}) == expected_targets


def test_release_evidence_accepts_independent_component_versions(tmp_path: Path) -> None:
    """Schema 5 应验签由根部署版本认证的独立组件版本组合。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=now + timedelta(days=1),
        schema_version=5,
    )

    evidence = verify_device_control_release_evidence(
        evidence_path=str(evidence_path),
        public_key_base64=public_key,
        now=now,
    )

    assert evidence.distribution_version == "9.8.7"
    assert evidence.components is not None
    assert evidence.components["agent-remote-node"].version == "2.3.4"
    assert evidence.components["agent-remote-device"].version == "5.6.7"


def test_release_evidence_rejects_mismatched_server_component() -> None:
    """组合清单中的 Server 版本必须与运行时绑定版本一致。"""

    now = datetime.now(UTC)
    target_digests = {
        "linux-amd64-glibc": "a" * 64,
        "linux-arm64-glibc": "b" * 64,
        "linux-amd64-musl": "c" * 64,
        "linux-arm64-musl": "d" * 64,
    }
    components = {
        name: ReleaseComponentIdentity(
            repository=f"Agent-Remote/{name}",
            version="1.2.3",
            commit="a" * 40,
            release_workflow="release.yml",
        )
        for name in (
            "agent-remote-server",
            "agent-remote-node",
            "agent-remote-cli",
            "agent-remote-admin-web",
            "agent-remote-device",
        )
    }
    with pytest.raises(ValueError, match="server component version does not match"):
        DeviceControlReleaseEvidence(
            schema_version=5,
            release_profile="community-local-trust",
            production_ready=True,
            apple_notarized=False,
            public_distribution=False,
            manual_trust_required=True,
            release_version=__version__,
            distribution_version="9.8.7",
            release_manifest_sha256=_DIGEST,
            components=components,
            issued_at=now,
            expires_at=now + timedelta(days=1),
            server_sha256=_DIGEST,
            node_artifacts_sha256=target_digests,
            application_sha256=_DIGEST,
            proxy_artifacts_sha256=target_digests,
            sbom_sha256=_DIGEST,
            provenance_sha256=_DIGEST,
            signing_notarization_sha256=_DIGEST,
            community_signing_sha256=_DIGEST,
            automated_release_checks_sha256=_DIGEST,
            risk_acceptance_sha256=_DIGEST,
            ci_run_url="https://ci.example.test/runs/mismatch",
            signature="test-only",
        )


def test_multi_arch_release_evidence_rejects_missing_target() -> None:
    """Schema 3 缺少任一受支持 Node target 时必须拒绝。"""

    with pytest.raises(ValueError, match="every supported Node target"):
        DeviceControlReleaseEvidence(
            schema_version=3,
            release_profile="community-local-trust",
            production_ready=True,
            apple_notarized=False,
            public_distribution=False,
            manual_trust_required=True,
            release_version=__version__,
            issued_at=datetime(2026, 7, 31, tzinfo=UTC),
            expires_at=datetime(2026, 8, 1, tzinfo=UTC),
            server_sha256=_DIGEST,
            application_sha256=_DIGEST,
            node_artifacts_sha256={"linux-amd64-glibc": _DIGEST},
            proxy_artifacts_sha256={"linux-amd64-glibc": _DIGEST},
            sbom_sha256=_DIGEST,
            provenance_sha256=_DIGEST,
            signing_notarization_sha256=_DIGEST,
            community_signing_sha256=_DIGEST,
            automated_release_checks_sha256=_DIGEST,
            risk_acceptance_sha256=_DIGEST,
            ci_run_url="https://ci.example.test/runs/incomplete",
            signature="pending",
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"apple_notarized": True}, "community local-trust"),
        ({"public_distribution": True}, "community local-trust"),
        ({"manual_trust_required": False}, "community local-trust"),
        ({"production_ready": False}, "not production ready"),
        ({"release_profile": "apple-developer-id"}, "community local-trust"),
        ({"security_review_sha256": _DIGEST}, "community local-trust"),
    ],
)
def test_community_release_evidence_rejects_conflicting_trust_claims(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    """Community 清单不得伪装成 Apple 公证或公开可信分发。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    create_signed_evidence(evidence_path, expires_at=now + timedelta(days=1))
    content = json.loads(evidence_path.read_text(encoding="utf-8"))
    content.update(changes)
    content["signature"] = "pending"

    with pytest.raises(ValueError, match=message):
        DeviceControlReleaseEvidence.model_validate(content)


def test_release_evidence_rejects_tampering(tmp_path: Path) -> None:
    """签名后的任何证据字段变更都必须拒绝。"""

    now = datetime(2026, 7, 31, tzinfo=UTC)
    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(evidence_path, expires_at=now + timedelta(days=1))
    content = json.loads(evidence_path.read_text(encoding="utf-8"))
    content["community_signing_sha256"] = "b" * 64
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
            '"schema_version": 2',
            '"schema_version": 2, "schema_version": 2',
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


def test_production_default_v2_accepts_general_release_evidence(tmp_path: Path) -> None:
    """生产默认 v2 只依赖当前通用发布证据，不要求专项验收摘要。"""

    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        schema_version=1,
        apple_profile=True,
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

    assert app.state.settings.device_control_v2_enabled is True
    assert app.state.device_control_release_evidence.computer_use_v2_evidence_sha256 is None


def test_production_preserves_optional_v2_evidence_metadata(tmp_path: Path) -> None:
    """专项 v2 证据继续作为可选质量元数据被验证并保留。"""

    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        schema_version=1,
        apple_profile=True,
        computer_use_v2_evidence_sha256="b" * 64,
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

    assert app.state.device_control_release_evidence.computer_use_v2_evidence_sha256 == "b" * 64


def test_production_accepts_optional_community_schema_v4_evidence(tmp_path: Path) -> None:
    """Community schema v4 继续作为可选的运行时验收记录。"""

    evidence_path = tmp_path / "release-evidence.json"
    public_key = create_signed_evidence(
        evidence_path,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        schema_version=4,
        computer_use_v2_evidence_sha256="b" * 64,
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

    evidence = app.state.device_control_release_evidence
    assert evidence.schema_version == 4
    assert evidence.release_profile == "community-local-trust"
    assert evidence.computer_use_v2_evidence_sha256 == "b" * 64


def test_legacy_community_evidence_cannot_claim_computer_use_v2_approval() -> None:
    """旧 Community schema 的风险接受不能替代 Computer Use v2 专项证据。"""

    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="cannot authorize Computer Use v2"):
        DeviceControlReleaseEvidence(
            schema_version=2,
            release_profile="community-local-trust",
            production_ready=True,
            apple_notarized=False,
            public_distribution=False,
            manual_trust_required=True,
            release_version=__version__,
            issued_at=now,
            expires_at=now + timedelta(days=1),
            server_sha256=_DIGEST,
            node_sha256=_DIGEST,
            application_sha256=_DIGEST,
            proxy_sha256=_DIGEST,
            sbom_sha256=_DIGEST,
            provenance_sha256=_DIGEST,
            signing_notarization_sha256=_DIGEST,
            computer_use_v2_evidence_sha256=_DIGEST,
            community_signing_sha256=_DIGEST,
            automated_release_checks_sha256=_DIGEST,
            risk_acceptance_sha256=_DIGEST,
            ci_run_url="https://ci.example.test/runs/community-v2",
            signature="test-only",
        )


def test_community_schema_v4_requires_computer_use_v2_evidence() -> None:
    """Schema v4 不允许省略专项证据摘要后退化为普通 Community 清单。"""

    now = datetime.now(UTC)
    target_digests = {
        "linux-amd64-glibc": "a" * 64,
        "linux-arm64-glibc": "b" * 64,
        "linux-amd64-musl": "c" * 64,
        "linux-arm64-musl": "d" * 64,
    }
    with pytest.raises(ValueError, match="schema version 4 requires Computer Use v2 evidence"):
        DeviceControlReleaseEvidence(
            schema_version=4,
            release_profile="community-local-trust",
            production_ready=True,
            apple_notarized=False,
            public_distribution=False,
            manual_trust_required=True,
            release_version=__version__,
            issued_at=now,
            expires_at=now + timedelta(days=1),
            server_sha256=_DIGEST,
            node_artifacts_sha256=target_digests,
            application_sha256=_DIGEST,
            proxy_artifacts_sha256=target_digests,
            sbom_sha256=_DIGEST,
            provenance_sha256=_DIGEST,
            signing_notarization_sha256=_DIGEST,
            community_signing_sha256=_DIGEST,
            automated_release_checks_sha256=_DIGEST,
            risk_acceptance_sha256=_DIGEST,
            ci_run_url="https://ci.example.test/runs/community-v4-empty",
            signature="test-only",
        )


def test_computer_use_v2_evidence_digest_must_be_sha256() -> None:
    """专项摘要必须使用规范小写 SHA-256。"""

    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        DeviceControlReleaseEvidence(
            schema_version=1,
            release_version=__version__,
            issued_at=now,
            expires_at=now + timedelta(days=1),
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
            computer_use_v2_evidence_sha256="INVALID",
            ci_run_url="https://ci.example.test/runs/apple-v2",
            signature="test-only",
        )


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
        schema_version=2,
        release_profile="community-local-trust",
        production_ready=True,
        apple_notarized=False,
        public_distribution=False,
        manual_trust_required=True,
        release_version=__version__,
        issued_at=now - timedelta(days=2),
        expires_at=now - timedelta(seconds=1),
        server_sha256=_DIGEST,
        node_sha256=_DIGEST,
        application_sha256=_DIGEST,
        proxy_sha256=_DIGEST,
        sbom_sha256=_DIGEST,
        provenance_sha256=_DIGEST,
        signing_notarization_sha256=_DIGEST,
        community_signing_sha256=_DIGEST,
        automated_release_checks_sha256=_DIGEST,
        risk_acceptance_sha256=_DIGEST,
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
