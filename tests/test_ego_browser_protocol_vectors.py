import base64
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel

from agent_remote_server.ego_browser.relay import parse_outer_envelope
from agent_remote_server.services.ego_browser import _verify_pop


class _ProofVectorPayload(BaseModel):
    """定义共享设备 PoP 向量使用的强类型 payload。"""

    allowlist_revision: int
    generation: int
    learning_bundle_digest: str | None
    signer_certificate_sha256: str


def _vectors() -> dict[str, object]:
    path = (
        Path(__file__).resolve().parents[2]
        / "agent-remote-ego-browser"
        / "protocol"
        / "test-vectors"
        / "ego-browser-bridge-v1.json"
    )
    if not path.is_file():
        pytest.skip("authoritative ego-browser protocol vectors are not checked out")
    return json.loads(path.read_bytes())


def test_shared_ego_browser_protocol_vectors() -> None:
    """Python relay 必须与共享 canonical JSON 和 outer envelope 向量一致。"""

    vectors = _vectors()
    assert vectors["schema_version"] == 1
    canonical_cases = vectors["canonical_json"]
    assert isinstance(canonical_cases, list)
    for case in canonical_cases:
        assert isinstance(case, dict)
        assert (
            json.dumps(case["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            == case["canonical"]
        )

    proof = vectors["device_pop_v2"]
    assert isinstance(proof, dict)
    proof_payload = _ProofVectorPayload.model_validate(proof["payload"])
    public_key = base64.urlsafe_b64decode(str(proof["public_key"]) + "=")
    assert _verify_pop(
        public_key=public_key,
        challenge=str(proof["challenge"]),
        signature=str(proof["signature"]),
        device_id=UUID(str(proof["device_id"])),
        device_generation=int(proof["device_generation"]),
        operation_generation=int(proof["operation_generation"]),
        release_profile=str(proof["release_profile"]),
        credential_profile=str(proof["credential_profile"]),
        server_host=str(proof["server_host"]),
        operation=str(proof["operation"]),
        binding_id=UUID(str(proof["binding_id"])),
        payload=proof_payload,
    )
    assert not _verify_pop(
        public_key=public_key,
        challenge=str(proof["challenge"]),
        signature=str(proof["signature"]),
        device_id=UUID(str(proof["device_id"])),
        device_generation=int(proof["device_generation"]),
        operation_generation=int(proof["operation_generation"]),
        release_profile=str(proof["release_profile"]),
        credential_profile=str(proof["credential_profile"]),
        server_host=str(proof["server_host"]),
        operation="renew_binding",
        binding_id=UUID(str(proof["binding_id"])),
        payload=proof_payload,
    )

    valid_outer = vectors["valid_outer"]
    assert isinstance(valid_outer, dict)
    encoded = json.dumps(valid_outer, separators=(",", ":")).encode()
    assert parse_outer_envelope(encoded, maximum_bytes=16 * 1024 * 1024) == valid_outer

    valid_cancel_outer = vectors["valid_cancel_outer"]
    assert isinstance(valid_cancel_outer, dict)
    encoded_cancel = json.dumps(valid_cancel_outer, separators=(",", ":")).encode()
    assert (
        parse_outer_envelope(encoded_cancel, maximum_bytes=16 * 1024 * 1024) == valid_cancel_outer
    )

    invalid_json = vectors["invalid_outer_json"]
    assert isinstance(invalid_json, list)
    for raw in invalid_json:
        assert isinstance(raw, str)
        with pytest.raises(ValueError):
            parse_outer_envelope(raw.encode(), maximum_bytes=16 * 1024 * 1024)

    invalid_outer = vectors["invalid_outer"]
    assert isinstance(invalid_outer, list)
    for value in invalid_outer:
        with pytest.raises(ValueError):
            parse_outer_envelope(
                json.dumps(value, separators=(",", ":")).encode(),
                maximum_bytes=16 * 1024 * 1024,
            )


def test_authoritative_ego_browser_schemas_forbid_unknown_fields() -> None:
    """协议发布的每个 JSON Schema 都必须要求完整字段集并拒绝未知字段。"""

    root = Path(__file__).resolve().parents[2] / "agent-remote-ego-browser" / "protocol" / "schemas"
    if not root.is_dir():
        pytest.skip("authoritative ego-browser schemas are not checked out")
    for path in sorted(root.glob("*.schema.json")):
        schema = json.loads(path.read_bytes())
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
