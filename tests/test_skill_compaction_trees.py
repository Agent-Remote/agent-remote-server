"""
验证完整树整理保持权限、空目录、受管依赖以及直接和间接链接连通单元。
"""

from uuid import uuid4

import pytest
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.compaction.plan import CompactionMember, compact_tree


def member(name: str) -> CompactionMember:
    """
    每个名称分配独立来源和内容身份，避免以名字代替选择授权。

    :param name (str): 顶层目录名
    :return CompactionMember: 精确测试成员
    """
    return CompactionMember(name, uuid4(), uuid4())


def test_compaction_preserves_exact_modes_empty_directories_and_runtime_dependencies() -> None:
    """
    删除一个独立旧根不会改变同前缀根、二进制内容、权限、空目录或外部依赖身份。
    """
    old = member("old")
    keep = member("older")
    kept = (
        SkillTreeEntry(path="older", kind="directory", mode=0o700),
        SkillTreeEntry(path="older/empty", kind="directory", mode=0o711),
        file_entry(b"\x00\xff", path="older/program").model_copy(update={"mode": 0o755}),
        SkillTreeEntry(
            path="older/python",
            kind="runtime_link",
            mode=0o777,
            target="/usr/bin/python3",
            dependency="python3",
        ),
    )
    tree = SkillTreeManifest(
        entries=(SkillTreeEntry(path="old", kind="directory", mode=0o755), *kept)
    )
    result, removed, blocked = compact_tree(
        tree, (old, keep), frozenset({old.checkpoint_id}), frozenset({"older"})
    )
    assert result.entries == kept and removed == (old,) and not blocked


@pytest.mark.parametrize("selection", ["partial", "complete", "protected_alias"])
def test_compaction_respects_indirect_link_units_and_protected_view_alias(selection: str) -> None:
    """
    链式相对链接连接全部依赖，整组选择才可删除，另一受保护视图即使名字相同也会阻断。

    :param selection (str): 部分选择、完整选择或另一受保护视图
    """
    a, b, c = (member(name) for name in ("a", "b", "c"))
    tree = SkillTreeManifest(
        entries=(
            SkillTreeEntry(path="a", kind="directory", mode=0o755),
            SkillTreeEntry(path="a/link", kind="symlink", mode=0o777, target="../b/link"),
            SkillTreeEntry(path="b", kind="directory", mode=0o755),
            SkillTreeEntry(path="b/link", kind="symlink", mode=0o777, target="../c/data"),
            SkillTreeEntry(path="c", kind="directory", mode=0o755),
            file_entry(b"shared", path="c/data"),
            file_entry(b"untouched auxiliary", path="root-data"),
        )
    )
    selected = (
        frozenset({a.checkpoint_id})
        if selection == "partial"
        else frozenset({a.checkpoint_id, b.checkpoint_id, c.checkpoint_id})
    )
    preserved = frozenset({"a"}) if selection == "protected_alias" else frozenset()
    result, removed, blocked = compact_tree(tree, (a, b, c), selected, preserved)
    if selection == "complete":
        assert result.entries == (tree.entries[-1],) and removed == (a, b, c) and not blocked
    else:
        assert result == tree and not removed
        assert {row.checkpoint_id for row in blocked} == selected
