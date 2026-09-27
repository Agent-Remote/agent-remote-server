"""
验证 Go 生成且 Rust 实际读取的同一冻结导出字节，防止三端清单或整数语义漂移。
"""

import io
import json
from pathlib import Path

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_node_export import NodeExportBinding
from agent_remote_server.skill_manager.manifest import manifest_digest, verify_file_content


def test_go_stream_matches_server_binding_manifest_and_complete_objects() -> None:
    """
    从真实 Go 流逐帧验证大整数身份、去重文件和完整结束，完全保留原始字节。
    """
    stream = io.BytesIO(
        (Path(__file__).parent / "fixtures/skills/frozen-export-v1.bin").read_bytes()
    )
    assert stream.read(8) == b"ARSKEX\x00\x01"
    header = json.loads(stream.read(int.from_bytes(stream.read(4), "big")))
    binding = NodeExportBinding.model_validate(header["binding"])
    assert binding.library_generation == 9007199254740993
    assert binding.directory_epoch == 9007199254740995
    manifest = SkillTreeManifest.model_validate(header["manifest"])
    assert manifest_digest(manifest) == header["tree_digest"]
    objects = {entry.sha256: entry for entry in manifest.entries if entry.kind == "file"}
    assert len(objects) == header["file_objects"] == 2
    for digest in sorted(objects):
        entry = objects[digest]
        verify_file_content(entry, stream.read(entry.size))
        assert stream.read(1) == b"\x01"
    complete = json.loads(stream.read(int.from_bytes(stream.read(4), "big")))
    assert complete == {
        "version": 1,
        "tree_digest": header["tree_digest"],
        "file_objects": 2,
        "complete": True,
    }
    assert stream.read() == b""
