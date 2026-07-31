import base64
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_remote_server import __version__
from agent_remote_server.device_control_release import verify_device_control_release_evidence

_DIGEST = "a" * 64


def write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    """写入测试专用的 owner-only Ed25519 私钥。"""

    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def write_draft(path: Path) -> None:
    """写入当前版本的测试发布证据 draft。"""

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
                "ci_run_url": "https://ci.example.test/runs/456",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def run_creator(draft: Path, private_key: Path, output: Path) -> subprocess.CompletedProcess[str]:
    """运行发布证据生成器并返回完成结果。"""

    return subprocess.run(
        [
            sys.executable,
            "scripts/create_device_control_release_evidence.py",
            "--draft",
            str(draft),
            "--private-key",
            str(private_key),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_creator_writes_a_verifiable_owner_only_manifest(tmp_path: Path) -> None:
    """生成器应写出可由生产验证器接受的 owner-only 清单。"""

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


def test_creator_rejects_group_readable_key_and_existing_output(tmp_path: Path) -> None:
    """生成器必须拒绝宽松私钥权限和覆盖已有输出。"""

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
