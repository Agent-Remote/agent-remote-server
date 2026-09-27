"""
验证完整目录组合中的跨项依赖、根级数据与单项配额。
"""

import pytest
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.skill_manager.materialization import (
    MaterializationError,
    SkillSubtree,
    compose_directory,
)


def linked_tree() -> SkillTreeManifest:
    """
    建立两个互相关联的有效条目与独立根级辅助文件。

    :return SkillTreeManifest: 可在完整目录内解析的跨项链接树
    """
    return SkillTreeManifest(
        entries=(
            SkillTreeEntry(path="alpha", kind="directory", mode=0o700),
            SkillTreeEntry(path="alpha/data", kind="symlink", mode=0o777, target="../beta/data"),
            SkillTreeEntry(path="beta", kind="directory", mode=0o755),
            file_entry(b"shared", path="beta/data"),
            file_entry(b"root", path="notes"),
        )
    )


def test_filter_revalidates_dependencies_without_reenabling_target() -> None:
    """
    缺少已停用依赖必须失败，不能自动复制目标内容或重新启用它。
    """
    source = linked_tree()
    with pytest.raises(MaterializationError) as error:
        compose_directory(source, {"alpha", "beta"}, [SkillSubtree("alpha", "alpha", source)], 100)
    assert error.value.code == "STATE_DEPENDENCY_MISSING"
    assert len(source.entries) == 5


def test_complete_views_preserve_links_permissions_and_root_data() -> None:
    """
    子树视图只在最终完整目录验证，根级数据和原权限均保留。
    """
    source = linked_tree()
    result = compose_directory(
        source,
        {"alpha", "beta"},
        [SkillSubtree("alpha", "alpha", source), SkillSubtree("beta", "beta", source)],
        100,
    )
    assert result == source
    empty = compose_directory(source, {"alpha", "beta"}, [], 100)
    assert empty.entries == (source.entries[-1],)


def test_new_package_cannot_overwrite_anonymous_root_data() -> None:
    """
    安装与同名辅助数据冲突必须显式处理，禁止无声覆盖。
    """
    source = linked_tree()
    package = SkillTreeManifest(entries=(file_entry(b"package"),))
    with pytest.raises(MaterializationError) as error:
        compose_directory(source, {"alpha", "beta"}, [SkillSubtree("notes", "", package)], 100)
    assert error.value.code == "STATE_SCOPE_MISMATCH"


def test_subtree_quota_counts_expanded_paths_instead_of_unique_objects() -> None:
    """
    同字节出现在不同路径时分别计入副本大小，不能用对象去重绕过限制。
    """
    package = SkillTreeManifest(
        entries=(file_entry(b"same", path="a"), file_entry(b"same", path="b"))
    )
    with pytest.raises(MaterializationError) as error:
        compose_directory(SkillTreeManifest(), set(), [SkillSubtree("alpha", "", package)], 7)
    assert error.value.code == "QUOTA_EXCEEDED"


@pytest.mark.parametrize("name", ["ego-browser", "agent-remote-device", "bad/path"])
def test_reserved_names_cannot_be_materialized(name: str) -> None:
    """
    用户数据组合不能成为覆盖系统路径的入口。

    :param name (str): 非法用户名称
    """
    with pytest.raises(MaterializationError):
        compose_directory(
            SkillTreeManifest(), set(), [SkillSubtree(name, "", SkillTreeManifest())], 100
        )


def test_root_auxiliary_quota_applies_before_new_session() -> None:
    """
    启动新副本也必须执行根级聚合额度，不能只限制已知技能条目。
    """
    directory = SkillTreeManifest(
        entries=(file_entry(b"1234", path="a"), file_entry(b"5678", path="b"))
    )
    with pytest.raises(MaterializationError) as error:
        compose_directory(directory, set(), [], 7)
    assert error.value.code == "QUOTA_EXCEEDED"
