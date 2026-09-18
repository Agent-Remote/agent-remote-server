"""
验证设备控制发布证据生成器。
"""

import base64
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_remote_server import __version__
from agent_remote_server.device_control.release import verify_device_control_release_evidence
from agent_remote_server.ego_browser.release_policy import (
    EGO_BROWSER_LOCAL_RUNTIME_VERSION,
    EGO_BROWSER_PROTOCOL_VERSION,
    EGO_BROWSER_SKILL_COMMIT,
    EGO_BROWSER_SKILL_TREE_SHA256,
    EGO_BROWSER_SKILL_VERSION,
    EGO_BROWSER_WRAPPER_VERSION,
)

_DIGEST = "a" * 64


def write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    """
    写入测试专用的 owner-only Ed25519 私钥。

    :param path (Path): 路径
    :param key (Ed25519PrivateKey): 键
    """

    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def write_draft(path: Path) -> None:
    """
    写入当前版本的测试发布证据 draft。

    :param path (Path): 路径
    """

    now = datetime.now(UTC)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_version": __version__,
                "issued_at": now.isoformat(),
                "expires_at": (now + timedelta(days=1)).isoformat(),
                "server_sha256": _DIGEST,
                "node_sha256": _DIGEST,
                "application_sha256": _DIGEST,
                "proxy_sha256": _DIGEST,
                "sbom_sha256": _DIGEST,
                "provenance_sha256": _DIGEST,
                "security_tests_sha256": _DIGEST,
                "security_review_sha256": _DIGEST,
                "signing_notarization_sha256": _DIGEST,
                "outbound_policy_sha256": _DIGEST,
                "local_claude_isolation_sha256": _DIGEST,
                "stop_revocation_sha256": _DIGEST,
                "compatibility_sha256": _DIGEST,
                "computer_use_v2_evidence_sha256": _DIGEST,
                "ci_run_url": "https://ci.example.test/runs/456",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def write_permanent_draft(path: Path) -> None:
    """
    写入不含过期时间、绑定根版本组合的 schema 9 draft。

    :param path (Path): 路径
    """

    components = {
        name: {
            "repository": f"Agent-Remote/{name}",
            "version": version,
            "commit": character * 40,
            "release_workflow": "release.yml",
        }
        for name, version, character in (
            ("agent-remote-server", __version__, "1"),
            ("agent-remote-node", "2.3.4", "2"),
            ("agent-remote-cli", "3.4.5", "3"),
            ("agent-remote-admin-web", "4.5.6", "4"),
            ("agent-remote-device", "5.6.7", "5"),
        )
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 9,
                "release_version": __version__,
                "issued_at": "2026-07-31T00:00:00+00:00",
                "distribution_version": "9.8.7",
                "release_manifest_sha256": _DIGEST,
                "components": components,
                "server_sha256": _DIGEST,
                "node_sha256": _DIGEST,
                "application_sha256": _DIGEST,
                "proxy_sha256": _DIGEST,
                "sbom_sha256": _DIGEST,
                "provenance_sha256": _DIGEST,
                "security_tests_sha256": _DIGEST,
                "security_review_sha256": _DIGEST,
                "signing_notarization_sha256": _DIGEST,
                "outbound_policy_sha256": _DIGEST,
                "local_claude_isolation_sha256": _DIGEST,
                "stop_revocation_sha256": _DIGEST,
                "compatibility_sha256": _DIGEST,
                "ci_run_url": "https://ci.example.test/runs/permanent",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def write_bridge_profile_draft(path: Path) -> dict[str, object]:
    """
    写入携带完整根仓库 schema 4 Bridge 身份的 Community 草稿。

    :param path (Path): 草稿路径
    :return dict[str, object]: 未签名证据字段
    """

    write_permanent_draft(path)
    draft = json.loads(path.read_text(encoding="utf-8"))
    components = cast(dict[str, dict[str, object]], draft["components"])
    version = EGO_BROWSER_WRAPPER_VERSION
    components["agent-remote-ego-browser"] = {
        "repository": "Agent-Remote/agent-remote-ego-browser",
        "version": version,
        "commit": "6" * 40,
        "release_workflow": "release.yml",
        "release_published": True,
        "profile": "community-local-trust",
        "signing_type": "project-self-signed",
        "signer_certificate_sha256": "f" * 64,
        "production_ready": True,
        "readiness_blockers": [],
        "apple_notarized": False,
        "public_distribution": False,
        "hardened_runtime": True,
        "nested_signatures_verified": True,
        "outbound_policy": "application-enforced",
        "credential_profile": "community_file",
        "learning_bundle_digest": "a" * 64,
        "learning_bundle_signing_key_id": "ego-browser-learning-2026-09",
        "skill_version": EGO_BROWSER_SKILL_VERSION,
        "skill_commit": EGO_BROWSER_SKILL_COMMIT,
        "skill_tree_sha256": EGO_BROWSER_SKILL_TREE_SHA256,
        "local_ego_browser_runtime_version": EGO_BROWSER_LOCAL_RUNTIME_VERSION,
        "protocol_version": EGO_BROWSER_PROTOCOL_VERSION,
        "profile_id": "community-local-trust",
        "profile_version": version,
        "bridge_version": version,
        "bridge_protocol_version": EGO_BROWSER_PROTOCOL_VERSION,
        "ego_lite_runtime_version": EGO_BROWSER_LOCAL_RUNTIME_VERSION,
        "wrapper_version": version,
        "artifact_url": (
            "https://github.com/Agent-Remote/agent-remote-ego-browser/releases/download/"
            f"v{version}/agent-remote-ego-browser-macos-universal-{version}.tar.gz"
        ),
        "artifact_sha256": "2" * 64,
        "bridge_manifest_sha256": "1" * 64,
        "ego_lite_installer_url": (
            "https://raw.githubusercontent.com/citrolabs/ego-lite/"
            f"{EGO_BROWSER_SKILL_COMMIT}/skills/ego-browser/scripts/install.sh"
        ),
        "ego_lite_installer_commit": EGO_BROWSER_SKILL_COMMIT,
        "ego_lite_installer_sha256": "3" * 64,
        "valid_platforms": ["macos"],
        "allowed_server_origins": ["$active_login_origin"],
        "admission_policy_ref": "server-policy:ego-browser-v1",
        "issued_at": "2026-09-18T03:33:57Z",
        "replaces_profile": "community-local-trust@0.1.12",
    }
    draft.update(
        {
            "release_profile": "community-local-trust",
            "apple_notarized": False,
            "public_distribution": False,
            "manual_trust_required": True,
            "node_sha256": None,
            "proxy_sha256": None,
            "node_artifacts_sha256": {
                target: _DIGEST
                for target in (
                    "linux-amd64-glibc",
                    "linux-arm64-glibc",
                    "linux-amd64-musl",
                    "linux-arm64-musl",
                )
            },
            "proxy_artifacts_sha256": {
                target: _DIGEST
                for target in (
                    "linux-amd64-glibc",
                    "linux-arm64-glibc",
                    "linux-amd64-musl",
                    "linux-arm64-musl",
                )
            },
            "security_tests_sha256": None,
            "security_review_sha256": None,
            "outbound_policy_sha256": None,
            "local_claude_isolation_sha256": None,
            "stop_revocation_sha256": None,
            "compatibility_sha256": None,
            "community_signing_sha256": _DIGEST,
            "automated_release_checks_sha256": _DIGEST,
            "risk_acceptance_sha256": _DIGEST,
            "ego_browser_release_manifest_sha256": "1" * 64,
            "ego_browser_release_archive_sha256": "2" * 64,
            "ego_browser_signing_evidence_sha256": "3" * 64,
            "ego_browser_learning_bundle_sha256": "a" * 64,
            "ego_browser_sigstore_sha256": "4" * 64,
            "ego_browser_provenance_sha256": "5" * 64,
        }
    )
    path.write_text(json.dumps(draft, sort_keys=True), encoding="utf-8")
    return draft


def run_creator(
    draft: Path,
    private_key: Path,
    output: Path,
    public_key_output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    运行发布证据生成器并返回完成结果。

    :param draft (Path): 草稿
    :param private_key (Path): 私钥
    :param output (Path): 输出
    :param public_key_output (Path | None): 公钥输出
    :return subprocess.CompletedProcess[str]: 证据生成进程结果
    """

    command = [
        sys.executable,
        "scripts/create_device_control_release_evidence.py",
        "--draft",
        str(draft),
        "--private-key",
        str(private_key),
        "--output",
        str(output),
    ]
    if public_key_output is not None:
        command.extend(("--public-key-output", str(public_key_output)))
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def test_creator_writes_a_verifiable_owner_only_manifest(tmp_path: Path) -> None:
    """
    生成器应写出可由生产验证器接受的 owner-only 清单。

    :param tmp_path (Path): pytest 临时目录
    """

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "draft.json"
    output_path = tmp_path / "evidence.json"
    write_private_key(key_path, key)
    write_draft(draft_path)

    result = run_creator(draft_path, key_path, output_path)

    assert result.returncode == 0, result.stderr
    assert output_path.stat().st_mode & 0o777 == 0o600
    raw_public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = verify_device_control_release_evidence(
        evidence_path=str(output_path),
        public_key_base64=base64.b64encode(raw_public_key).decode("ascii"),
    )
    assert manifest.release_version == __version__


def test_creator_writes_permanent_schema_9_manifest(tmp_path: Path) -> None:
    """
    当前生成器必须输出与根版本绑定且永久有效的 schema 9 清单。

    :param tmp_path (Path): pytest 临时目录
    """

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "permanent-draft.json"
    output_path = tmp_path / "permanent-evidence.json"
    write_private_key(key_path, key)
    write_permanent_draft(draft_path)

    result = run_creator(draft_path, key_path, output_path)

    assert result.returncode == 0, result.stderr
    raw_manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert raw_manifest["schema_version"] == 9
    assert raw_manifest["distribution_version"] == "9.8.7"
    assert "expires_at" not in raw_manifest
    for field in (
        "node_sha256",
        "proxy_sha256",
        "security_tests_sha256",
        "security_review_sha256",
        "outbound_policy_sha256",
        "local_claude_isolation_sha256",
        "stop_revocation_sha256",
        "compatibility_sha256",
        "computer_use_v2_evidence_sha256",
    ):
        assert field in raw_manifest
    assert raw_manifest["computer_use_v2_evidence_sha256"] is None
    raw_public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = verify_device_control_release_evidence(
        evidence_path=str(output_path),
        public_key_base64=base64.b64encode(raw_public_key).decode("ascii"),
        now=datetime(2036, 7, 31, tzinfo=UTC),
    )
    assert manifest.expires_at is None


def test_creator_signs_complete_bridge_profile_without_losing_fields(tmp_path: Path) -> None:
    """
    完整根仓库 Bridge 配置应进入签名载荷并通过生产验签。

    :param tmp_path (Path): pytest 临时目录
    """

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "bridge-draft.json"
    output_path = tmp_path / "bridge-evidence.json"
    write_private_key(key_path, key)
    draft = write_bridge_profile_draft(draft_path)

    result = run_creator(draft_path, key_path, output_path)

    assert result.returncode == 0, result.stderr
    raw = json.loads(output_path.read_text(encoding="utf-8"))
    assert raw["components"] == draft["components"]
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = verify_device_control_release_evidence(
        evidence_path=str(output_path),
        public_key_base64=base64.b64encode(public_key).decode("ascii"),
    )
    assert manifest.components is not None
    draft_components = cast(dict[str, dict[str, object]], draft["components"])
    assert (
        manifest.components["agent-remote-ego-browser"].model_dump()
        == draft_components["agent-remote-ego-browser"]
    )


@pytest.mark.parametrize("field", ["profile_version", "artifact_sha256", "issued_at"])
def test_creator_rejects_incomplete_bridge_profile(tmp_path: Path, field: str) -> None:
    """
    根仓库 Bridge 配置缺少必需字段时不得签发证据。

    :param tmp_path (Path): pytest 临时目录
    :param field (str): 待删除的配置字段
    """

    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "bridge-draft.json"
    output_path = tmp_path / "bridge-evidence.json"
    write_private_key(key_path, Ed25519PrivateKey.generate())
    draft = write_bridge_profile_draft(draft_path)
    components = cast(dict[str, dict[str, object]], draft["components"])
    del components["agent-remote-ego-browser"][field]
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    result = run_creator(draft_path, key_path, output_path)

    assert result.returncode == 2
    assert not output_path.exists()


@pytest.mark.parametrize("field", ["unexpected_field", "artifact_sha256"])
def test_creator_rejects_unbound_bridge_profile_fields(tmp_path: Path, field: str) -> None:
    """
    未定义字段或与顶层证据不一致的归档摘要不得进入签名。

    :param tmp_path (Path): pytest 临时目录
    :param field (str): 待修改的配置字段
    """

    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "bridge-draft.json"
    output_path = tmp_path / "bridge-evidence.json"
    write_private_key(key_path, Ed25519PrivateKey.generate())
    draft = write_bridge_profile_draft(draft_path)
    components = cast(dict[str, dict[str, object]], draft["components"])
    components["agent-remote-ego-browser"][field] = "4" * 64
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    result = run_creator(draft_path, key_path, output_path)

    assert result.returncode == 2
    assert not output_path.exists()


def test_creator_writes_the_verified_raw_public_key(tmp_path: Path) -> None:
    """
    可选公钥输出必须对应实际签名密钥并保持 owner-only。

    :param tmp_path (Path): pytest 临时目录
    """

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "draft.json"
    output_path = tmp_path / "evidence.json"
    public_key_path = tmp_path / "public-key.txt"
    write_private_key(key_path, key)
    write_draft(draft_path)

    result = run_creator(draft_path, key_path, output_path, public_key_path)

    assert result.returncode == 0, result.stderr
    assert public_key_path.stat().st_mode & 0o777 == 0o600
    expected = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    encoded = public_key_path.read_text(encoding="ascii")
    assert encoded == base64.b64encode(expected).decode("ascii") + "\n"
    verify_device_control_release_evidence(
        evidence_path=str(output_path),
        public_key_base64=encoded.strip(),
    )


def test_creator_preserves_existing_public_key_output(tmp_path: Path) -> None:
    """
    已有公钥目标必须导致失败，且不得留下配套 manifest 或覆盖目标。

    :param tmp_path (Path): pytest 临时目录
    """

    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "draft.json"
    output_path = tmp_path / "evidence.json"
    public_key_path = tmp_path / "public-key.txt"
    write_private_key(key_path, Ed25519PrivateKey.generate())
    write_draft(draft_path)
    public_key_path.write_text("preserve\n", encoding="ascii")

    result = run_creator(draft_path, key_path, output_path, public_key_path)

    assert result.returncode == 2
    assert public_key_path.read_text(encoding="ascii") == "preserve\n"
    assert not output_path.exists()


def test_creator_rejects_group_readable_key_and_existing_output(tmp_path: Path) -> None:
    """
    生成器必须拒绝宽松私钥权限和覆盖已有输出。

    :param tmp_path (Path): pytest 临时目录
    """

    key_path = tmp_path / "release-key.pem"
    draft_path = tmp_path / "draft.json"
    output_path = tmp_path / "evidence.json"
    write_private_key(key_path, Ed25519PrivateKey.generate())
    write_draft(draft_path)
    key_path.chmod(0o640)

    unsafe_key = run_creator(draft_path, key_path, output_path)

    assert unsafe_key.returncode == 2
    assert not output_path.exists()
    key_path.chmod(0o600)
    output_path.write_text("preserve", encoding="utf-8")

    existing_output = run_creator(draft_path, key_path, output_path)

    assert existing_output.returncode == 2
    assert output_path.read_text(encoding="utf-8") == "preserve"
