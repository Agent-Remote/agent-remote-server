import argparse
import base64
import json
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from agent_remote_server.device_control_release import (
    DeviceControlReleaseEvidence,
    verify_device_control_release_evidence,
)

_MAXIMUM_DRAFT_BYTES = 65_536
_MAXIMUM_PRIVATE_KEY_BYTES = 16_384


def _private_regular_file(path: Path, maximum_bytes: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > maximum_bytes
    ):
        raise ValueError(f"unsafe private input file: {path.name}")
    return path.read_bytes()


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    key_data = _private_regular_file(path, _MAXIMUM_PRIVATE_KEY_BYTES)
    key = serialization.load_pem_private_key(key_data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("release evidence key must be an Ed25519 private key")
    return key


def _load_draft(path: Path) -> dict[str, object]:
    data = _private_regular_file(path, _MAXIMUM_DRAFT_BYTES)
    value = json.loads(data)
    if not isinstance(value, dict) or "signature" in value:
        raise ValueError("release evidence draft must be an unsigned JSON object")
    return value


def _write_new_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(data)
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _raw_public_key_base64(private_key: Ed25519PrivateKey) -> str:
    public_key: Ed25519PublicKey = private_key.public_key()
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def main() -> None:
    """
    创建并自检设备控制生产发布证据清单

    :raises SystemExit: 输入、密钥、清单或输出路径不符合安全要求
    """

    parser = argparse.ArgumentParser(
        description="Create a signed device-control release-evidence manifest."
    )
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        draft = _load_draft(args.draft)
        private_key = _load_private_key(args.private_key)
        unsigned = DeviceControlReleaseEvidence.model_validate({**draft, "signature": "pending"})
        signature = private_key.sign(unsigned.signing_payload())
        manifest = unsigned.model_copy(
            update={"signature": base64.b64encode(signature).decode("ascii")}
        )
        _write_new_file(args.output, manifest.encoded_manifest())
        try:
            verify_device_control_release_evidence(
                evidence_path=str(args.output),
                public_key_base64=_raw_public_key_base64(private_key),
            )
        except ValueError:
            args.output.unlink(missing_ok=True)
            raise
    except (OSError, ValueError, ValidationError) as exc:
        parser.exit(2, f"release evidence creation failed: {exc}\n")


if __name__ == "__main__":
    main()
