"""
验证技能目录清单的路径隔离、完整性和规范摘要。
"""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.skill_manager.manifest import manifest_digest, verify_file_content

VECTORS = json.loads((Path(__file__).parent / "fixtures/skills/manifest-v1.json").read_text())


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda case: case["name"])
def test_shared_manifest_digest_vectors(case: dict[str, object]) -> None:
    """
    对照跨三端固定向量验证目录身份。

    :param case (dict[str, object]): 有效清单向量
    """
    value = case["manifest_json"]
    assert isinstance(value, str)
    manifest = SkillTreeManifest.model_validate_json(value)
    assert manifest_digest(manifest) == case["tree_sha256"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda case: case["name"])
def test_shared_manifest_rejection_vectors(case: dict[str, object]) -> None:
    """
    对照跨三端固定向量拒绝无效清单。

    :param case (dict[str, object]): 无效清单向量
    """
    value = case["manifest_json"]
    assert isinstance(value, str)
    with pytest.raises(ValidationError):
        SkillTreeManifest.model_validate_json(value)


def file_entry(path: str, content: bytes = b"hello\n", mode: int = 0o644) -> SkillTreeEntry:
    """
    构建带真实内容摘要的文件测试条目。

    :param path (str): 相对文件路径
    :param content (bytes): 文件内容
    :param mode (int): 文件权限
    :return SkillTreeEntry: 可验证的文件条目
    """
    return SkillTreeEntry(
        path=path,
        kind="file",
        mode=mode,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_kind="binary" if b"\x00" in content else "text",
    )


def test_empty_manifest_has_fixed_protocol_digest() -> None:
    """
    确保空树使用固定协议前缀而不是任意语言的 JSON 编码。
    """
    assert (
        manifest_digest(SkillTreeManifest())
        == hashlib.sha256(b"agent-remote-skill-tree-v1\x00").hexdigest()
    )


@pytest.mark.parametrize(
    "path",
    ["", "/root/key", "../key", "a/../../key", "a//b", "a/./b", "a\\b", "a\x00b"],
)
def test_rejects_noncanonical_or_escaping_paths(path: str) -> None:
    """
    拒绝不能安全解释为相对路径的输入。

    :param path (str): 非法路径
    """
    with pytest.raises(ValidationError):
        file_entry(path)


def test_rejects_implicit_or_nondirectory_parents() -> None:
    """
    防止清单依靠隐式目录或把文件当作目录解包。
    """
    child = file_entry("scripts/run.py")
    with pytest.raises(ValidationError):
        SkillTreeManifest(entries=(child,))
    with pytest.raises(ValidationError):
        SkillTreeManifest(entries=(file_entry("scripts"), child))


def test_rejects_duplicates_and_unsorted_entries() -> None:
    """
    防止同名覆盖及不同排序得到不同的目录身份。
    """
    entry = file_entry("SKILL.md")
    with pytest.raises(ValidationError):
        SkillTreeManifest(entries=(entry, entry))
    with pytest.raises(ValidationError):
        SkillTreeManifest(entries=(file_entry("b"), file_entry("a")))


def test_resolves_internal_links_without_following_host_files() -> None:
    """
    允许目录内相对链接但拒绝越界和悬空目标。
    """
    directory = SkillTreeEntry(path="refs", kind="directory", mode=0o755)
    link = SkillTreeEntry(path="refs/main", kind="symlink", mode=0o777, target="../SKILL.md")
    manifest = SkillTreeManifest(entries=(file_entry("SKILL.md"), directory, link))
    assert len(manifest.entries) == 3
    for target in ("../../outside", "missing"):
        with pytest.raises(ValidationError):
            SkillTreeManifest(
                entries=(
                    file_entry("SKILL.md"),
                    directory,
                    link.model_copy(update={"target": target}),
                )
            )


def test_rejects_link_cycles() -> None:
    """
    防止清单中循环链接导致无限解析。
    """
    with pytest.raises(ValidationError):
        SkillTreeManifest(
            entries=(
                SkillTreeEntry(path="a", kind="symlink", mode=0o777, target="b"),
                SkillTreeEntry(path="b", kind="symlink", mode=0o777, target="a"),
            )
        )


def test_rejects_boolean_numeric_fields_and_unknown_fields() -> None:
    """
    保证三端不会把布尔值或额外字段解释为不同的协议。
    """
    entry = file_entry("SKILL.md").model_dump()
    for field in ("size", "mode"):
        with pytest.raises(ValidationError):
            SkillTreeEntry.model_validate({**entry, field: True})
    with pytest.raises(ValidationError):
        SkillTreeManifest.model_validate({"version": True, "entries": []})
    with pytest.raises(ValidationError):
        SkillTreeEntry.model_validate({**entry, "owner": "unexpected"})


def test_permission_and_content_classification_are_part_of_identity() -> None:
    """
    保证修改执行权限或内容类型不能复用原目录摘要。
    """
    entry = file_entry("run")
    plain = manifest_digest(SkillTreeManifest(entries=(entry,)))
    executable = manifest_digest(SkillTreeManifest(entries=(file_entry("run", mode=0o755),)))
    binary = manifest_digest(
        SkillTreeManifest(entries=(entry.model_copy(update={"content_kind": "binary"}),))
    )
    assert len({plain, executable, binary}) == 3


def test_verifies_payload_and_declared_text_classification() -> None:
    """
    上传内容必须同时符合长度、摘要和文本类型声明。
    """
    entry = file_entry("SKILL.md")
    verify_file_content(entry, b"hello\n")
    with pytest.raises(ValueError):
        verify_file_content(entry, b"other\n")
    content = b"bad\x00text"
    dishonest = file_entry("state", content).model_copy(update={"content_kind": "text"})
    with pytest.raises(ValueError):
        verify_file_content(dishonest, content)


def test_runtime_link_requires_explicit_dependency_identity() -> None:
    """
    外部运行时链接必须标记依赖而不能伪装为普通相对链接。
    """
    with pytest.raises(ValidationError):
        SkillTreeEntry(path="python", kind="symlink", mode=0o777, target="/usr/bin/python3")
    with pytest.raises(ValidationError):
        SkillTreeEntry(path="python", kind="runtime_link", mode=0o777, target="/usr/bin/python3")
    link = SkillTreeEntry(
        path="python",
        kind="runtime_link",
        mode=0o777,
        target="/usr/bin/python3",
        dependency="python3",
    )
    assert SkillTreeManifest(entries=(link,)).entries[0].dependency == "python3"
