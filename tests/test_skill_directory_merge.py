"""
验证账户目录按显式技能身份和跨条目链接组成保守合并单元。
"""

from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.skill_manager.directory_merge import merge_directory_manifests


def directory(files: dict[str, bytes], links: dict[str, str] | None = None) -> SkillTreeManifest:
    """
    构建包含显式父目录和相对链接的完整测试树。

    :param files (dict[str, bytes]): 文件完整内容
    :param links (dict[str, str] | None): 相对链接
    :return SkillTreeManifest: 完整有效清单
    """
    entries = {path: file_entry(content, path=path) for path, content in files.items()}
    entries.update(
        {
            path: SkillTreeEntry(path=path, kind="symlink", mode=0o777, target=target)
            for path, target in (links or {}).items()
        }
    )
    for path in tuple(entries):
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            entries.setdefault(parent, SkillTreeEntry(path=parent, kind="directory", mode=0o755))
    return SkillTreeManifest(
        entries=tuple(entries[path] for path in sorted(entries, key=str.encode))
    )


def test_independent_binary_and_text_changes_can_publish_atomically() -> None:
    """
    两个无链接技能各自单侧修改时，二进制不会污染整个账户目录。
    """
    base = directory({"memory/state.db": b"old\x00", "writer/script": b"old"})
    current = directory({"memory/state.db": b"new\x00", "writer/script": b"old"})
    incoming = directory({"memory/state.db": b"old\x00", "writer/script": b"new"})
    result = merge_directory_manifests(base, current, incoming, {"memory", "writer"})
    assert result.merged == directory({"memory/state.db": b"new\x00", "writer/script": b"new"})
    assert result.units == (("memory",), ("writer",)) and not result.conflicts


def test_linked_database_and_script_divergence_is_one_conflict() -> None:
    """
    脚本通过链接依赖另一技能数据库时，双方改动不能按文件拼接。
    """
    links = {"writer/database": "../memory/state.db"}
    base = directory({"memory/state.db": b"old\x00", "writer/script": b"old"}, links)
    current = directory({"memory/state.db": b"new\x00", "writer/script": b"old"}, links)
    incoming = directory({"memory/state.db": b"old\x00", "writer/script": b"new"}, links)
    result = merge_directory_manifests(base, current, incoming, {"memory", "writer"})
    assert result.merged is None
    assert result.units == (("memory", "writer"),)
    assert result.conflicts[0].reason == "opaque_divergence"
    assert result.conflicts[0].unit == ("memory", "writer")


def test_link_removed_on_one_side_still_protects_original_unit() -> None:
    """
    不能通过一侧删除跨项链接来拆开原本关联的不透明状态。
    """
    files = {"memory/state.db": b"old\x00", "writer/script": b"old"}
    links = {"writer/database": "../memory/state.db"}
    base = directory(files, links)
    current = directory({**files, "memory/state.db": b"new\x00"}, links)
    incoming = directory(files)
    result = merge_directory_manifests(base, current, incoming, {"memory", "writer"})
    assert result.merged is None and result.conflicts[0].unit == ("memory", "writer")


def test_link_chain_includes_intermediate_skill_identity() -> None:
    """
    不能只连接最终目标而漏掉提供中间链接的技能。
    """
    files = {"a/script": b"old", "b/script": b"old", "c/data.db": b"old\x00"}
    links = {"a/ref": "../b/ref", "b/ref": "../c/data.db"}
    base = directory(files, links)
    current = directory({**files, "b/script": b"updated"}, links)
    incoming = directory({**files, "c/data.db": b"new\x00"}, links)
    result = merge_directory_manifests(base, current, incoming, {"a", "b", "c"})
    assert result.units == (("a", "b", "c"),)
    assert result.merged is None and result.conflicts[0].reason == "opaque_divergence"


def test_auxiliary_data_is_one_unit_without_filename_identity_guessing() -> None:
    """
    未登记目录都属于同一根级辅助范围，不能凭目录名拆分数据库状态。
    """
    files = {"cache/state.db": b"old\x00", "notes/config": b"old", "skill/script": b"old"}
    base = directory(files)
    current = directory({**files, "cache/state.db": b"new\x00"})
    incoming = directory({**files, "notes/config": b"new"})
    result = merge_directory_manifests(base, current, incoming, {"skill"})
    assert result.units == ((".",), ("skill",))
    assert result.merged is None and result.conflicts[0].unit == (".",)


def test_root_link_connects_auxiliary_and_named_skill() -> None:
    """
    根级链接依赖技能时，辅助数据与该技能共同受到不透明保护。
    """
    files = {"cache.db": b"old\x00", "skill/script": b"old"}
    links = {"script-ref": "skill/script"}
    base = directory(files, links)
    current = directory({**files, "cache.db": b"new\x00"}, links)
    incoming = directory({**files, "skill/script": b"new"}, links)
    result = merge_directory_manifests(base, current, incoming, {"skill"})
    assert result.merged is None and result.conflicts[0].unit == (".", "skill")


def test_one_unit_conflict_suppresses_other_units_complete_changes() -> None:
    """
    任一技能发生冲突时不能返回其余技能的可发布部分树。
    """
    files = {"a/script": b"old", "b/script": b"old"}
    result = merge_directory_manifests(
        directory(files),
        directory({**files, "a/script": b"ours"}),
        directory({"a/script": b"theirs", "b/script": b"learned"}),
        {"a", "b"},
    )
    assert result.merged is None and result.conflicts[0].path == "a/script"
    assert result.conflicts[0].unit == ("a",)


def test_linked_text_changes_still_merge_without_opaque_state() -> None:
    """
    链接建立共同单元不禁止无冲突的普通文本三方合并。
    """
    files = {"a/script": b"old", "b/data": b"old"}
    links = {"a/ref": "../b/data"}
    result = merge_directory_manifests(
        directory(files, links),
        directory({**files, "a/script": b"new"}, links),
        directory({**files, "b/data": b"learned"}, links),
        {"a", "b"},
    )
    assert result.merged == directory({"a/script": b"new", "b/data": b"learned"}, links)


def test_individually_valid_link_changes_cannot_publish_a_combined_cycle() -> None:
    """
    两侧各自合法的链接变化若合并成循环，完整提交必须转为冲突。
    """
    base = directory({"a/data": b"old", "b/data": b"old"})
    current = directory({"b/data": b"old"}, {"a/data": "../b/data"})
    incoming = directory({"a/data": b"old"}, {"b/data": "../a/data"})
    result = merge_directory_manifests(base, current, incoming, {"a", "b"})
    assert result.merged is None and result.conflicts[0].reason == "invalid_tree"
    assert result.conflicts[0].unit == ("a", "b")
