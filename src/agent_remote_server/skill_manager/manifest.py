"""
实现技能清单的规范摘要与实际内容校验。
"""

import hashlib

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest


def manifest_digest(manifest: SkillTreeManifest) -> str:
    """
    生成与 Go 和 Rust 共享的第一版目录摘要。

    :param manifest (SkillTreeManifest): 已验证的完整清单
    :return str: 小写 SHA-256 摘要
    """
    digest = hashlib.sha256(b"agent-remote-skill-tree-v1\x00")
    for entry in manifest.entries:
        for value in (
            entry.path,
            entry.kind,
            str(entry.mode),
            str(entry.size),
            entry.sha256,
            entry.target,
            entry.content_kind,
            entry.dependency,
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\x00")
    return digest.hexdigest()


def verify_file_content(entry: SkillTreeEntry, content: bytes) -> None:
    """
    验证文件长度、摘要和文本类型均符合清单。

    :param entry (SkillTreeEntry): 原始文件元数据
    :param content (bytes): 待验证的完整文件内容
    :raises ValueError: 实际内容与声明不一致
    """
    if entry.kind != "file" or entry.size != len(content):
        raise ValueError("skill content size or entry kind does not match")
    if hashlib.sha256(content).hexdigest() != entry.sha256:
        raise ValueError("skill content digest does not match")
    try:
        content.decode("utf-8")
        is_text = b"\x00" not in content
    except UnicodeDecodeError:
        is_text = False
    if (entry.content_kind == "text") != is_text:
        raise ValueError("skill content classification does not match")
