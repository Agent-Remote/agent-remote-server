"""
验证技能三方合并不发布冲突或无效的部分目录树。
"""

import hashlib

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.skill_manager.merge import merge_manifests


def tree(files: dict[str, bytes]) -> SkillTreeManifest:
    """
    根据根级测试文件构建完整清单。

    :param files (dict[str, bytes]): 文件路径与字节内容
    :return SkillTreeManifest: 已验证的测试目录树
    """
    return SkillTreeManifest(
        entries=tuple(
            SkillTreeEntry(
                path=path,
                kind="file",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content_kind="binary" if b"\x00" in content else "text",
            )
            for path, content in sorted(files.items())
        )
    )


def test_merges_disjoint_changes_and_preserves_deletion() -> None:
    """
    非重叠文本变更与明确删除可以一次合并。
    """
    base = tree({"a": b"old", "b": b"old", "gone": b"delete"})
    current = tree({"a": b"new", "b": b"old"})
    incoming = tree({"a": b"old", "b": b"learned", "gone": b"delete"})
    result = merge_manifests(base, current, incoming)
    assert result.merged == tree({"a": b"new", "b": b"learned"})
    assert result.conflicts == ()


def test_same_path_conflict_never_exposes_partial_publishable_result() -> None:
    """
    同路径分歧不发布其他已经合并的文件。
    """
    result = merge_manifests(
        tree({"a": b"old", "b": b"old"}),
        tree({"a": b"left", "b": b"new"}),
        tree({"a": b"right", "b": b"old"}),
    )
    assert result.merged is None
    assert [conflict.path for conflict in result.conflicts] == ["a"]


def test_delete_against_modify_is_a_conflict() -> None:
    """
    一侧删除和另一侧修改不能采用最后写入获胜。
    """
    result = merge_manifests(tree({"a": b"old"}), tree({}), tree({"a": b"new"}))
    assert result.merged is None
    assert result.conflicts[0].reason == "changed_both"


def test_fast_forward_binary_state_does_not_require_conflict_resolution() -> None:
    """
    只有一个分支变更数据库时可以完整快进。
    """
    base = tree({"memory.db": b"old\x00"})
    incoming = tree({"memory.db": b"new\x00"})
    assert merge_manifests(base, base, incoming).merged == incoming
    assert merge_manifests(base, incoming, incoming).merged == incoming


def test_divergent_database_or_binary_changes_conflict_as_a_whole() -> None:
    """
    双侧变更涉及不透明状态时禁止逐文件拼接。
    """
    base = tree({"memory.db": b"plain header", "script": b"v1"})
    current = tree({"memory.db": b"changed header", "script": b"v1"})
    incoming = tree({"memory.db": b"plain header", "script": b"v2"})
    result = merge_manifests(base, current, incoming)
    assert result.merged is None
    assert result.conflicts[0].path == "."
    assert result.conflicts[0].reason == "opaque_divergence"


def test_directory_replacement_conflicting_with_child_edit_is_atomic() -> None:
    """
    父目录替换不能产生同时包含文件和其子路径的结果。
    """
    directory = SkillTreeEntry(path="a", kind="directory", mode=0o755)
    old_child = tree({"b": b"old"}).entries[0]
    child = old_child.model_copy(update={"path": "a/b"})
    base = SkillTreeManifest(entries=(directory, child))
    incoming = SkillTreeManifest(
        entries=(directory, tree({"b": b"new"}).entries[0].model_copy(update={"path": "a/b"}))
    )
    result = merge_manifests(base, tree({"a": b"replacement"}), incoming)
    assert result.merged is None
    assert result.conflicts
