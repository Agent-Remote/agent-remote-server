"""
验证迁移候选计算保持真实当前上下文和完整关联单元，不暴露部分目录。
"""

from dataclasses import replace

import pytest
from test_skill_directory_merge import directory

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.skill_manager.directory_merge import merge_directory_manifests
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.migration_resolution import (
    MigrationResolutionInputs,
    migration_resolution_unit,
    resolve_migration_conflicts,
)
from agent_remote_server.skill_manager.resolution import LoadedResolutionChoice


def ordinary() -> MigrationResolutionInputs:
    """
    独立目标有两个冲突和双方互不相交修改，目录另含独立来源。

    :return MigrationResolutionInputs: 完整独立迁移输入
    """
    base = directory({"a/doc": b"old", "a/other": b"old"})
    current = directory({"a/doc": b"upstream", "a/other": b"upstream", "a/new": b"new"})
    incoming = directory({"a/doc": b"learned", "a/other": b"learned", "a/memory": b"memory"})
    context = directory({"a/doc": b"shown", "b/doc": b"independent", "notes": b"aux"})
    return MigrationResolutionInputs(
        "a",
        frozenset({"a", "b"}),
        base,
        current,
        incoming,
        context,
        merge_directory_manifests(base, current, incoming, {"a"}).conflicts,
    )


def linked() -> MigrationResolutionInputs:
    """
    来源包含旧的独立上下文，当前侧只有目标新版原始包。

    :return MigrationResolutionInputs: 目标与辅助根关联的完整输入
    """
    base = directory({"a/doc": b"old"})
    current = directory({"a/doc": b"upstream"})
    incoming = directory(
        {"a/doc": b"learned", "state.db": b"old data", "b/doc": b"historical context"},
        {"a/data": "../state.db"},
    )
    context = directory({"a/doc": b"shown", "state.db": b"current data", "b/doc": b"today"})
    return MigrationResolutionInputs(
        "a",
        frozenset({"a", "b"}),
        base,
        current,
        incoming,
        context,
        (SkillMergeConflict(path="a", reason="source_conflict", unit=(".", "a")),),
    )


def choose(**values: object) -> LoadedResolutionChoice:
    """
    使用真实严格 schema 创建单次侧选择。

    :param values (object): 明确选择字段
    :return LoadedResolutionChoice: 不含人工树的选择
    """
    return LoadedResolutionChoice(SkillResolutionChoice.model_validate(values))


def custom(tree: SkillTreeManifest) -> LoadedResolutionChoice:
    """
    人工目录摘要绑定完整清单，范围外内容仍需经过保护检查。

    :param tree (SkillTreeManifest): 已验证完整人工树
    :return LoadedResolutionChoice: 明确目录选择
    """
    return LoadedResolutionChoice(
        SkillResolutionChoice(directory_tree_digest=manifest_digest(tree)), tree
    )


def test_partial_plan_retains_all_changes_until_every_conflict_is_selected() -> None:
    """
    部分计划只有剩余冲突，全部选择后保留双方非冲突变化和目录独立来源。
    """
    inputs = ordinary()
    first = choose(path="a/doc", use="current")
    result = resolve_migration_conflicts(inputs, [first])
    assert result.merged is None and [item.path for item in result.conflicts] == ["a/other"]
    result = resolve_migration_conflicts(inputs, [first, choose(path="a/other", use="incoming")])
    assert result.merged == directory(
        {
            "a/doc": b"upstream",
            "a/other": b"learned",
            "a/new": b"new",
            "a/memory": b"memory",
            "b/doc": b"independent",
            "notes": b"aux",
        }
    )


@pytest.mark.parametrize("use", ["current", "incoming"])
def test_whole_independent_choice_replaces_only_target_unit(use: str) -> None:
    """
    整项选择可以覆盖新版脚本，但无关目录内容始终保持账户当前版本。

    :param use (str): 目标完整侧
    """
    inputs = ordinary()
    result = resolve_migration_conflicts(inputs, [choose(use=use)])
    assert result.merged is not None
    selected = inputs.current if use == "current" else inputs.incoming
    assert (
        tuple(entry for entry in result.merged.entries if entry.path.split("/")[0] == "a")
        == selected.entries
    )
    assert tuple(
        entry for entry in result.merged.entries if entry.path.split("/")[0] != "a"
    ) == tuple(entry for entry in inputs.directory.entries if entry.path.split("/")[0] != "a")


def test_custom_file_and_side_choice_preserve_uncontested_state() -> None:
    """
    人工普通文件只替换冲突路径，不丢失双方其他文件。
    """
    inputs = ordinary()
    tree = directory({"resolved": b"chosen bytes"})
    file = LoadedResolutionChoice(
        SkillResolutionChoice(path="a/doc", file_tree_digest=manifest_digest(tree)), tree
    )
    result = resolve_migration_conflicts(inputs, [file, choose(path="a/other", use="current")])
    assert result.merged == directory(
        {
            "a/doc": b"chosen bytes",
            "a/other": b"upstream",
            "a/new": b"new",
            "a/memory": b"memory",
            "b/doc": b"independent",
            "notes": b"aux",
        }
    )


def test_linked_current_uses_current_directory_context_not_old_source_extras() -> None:
    """
    新版原始包不含辅助数据时，current 仍保留当前目录的关联辅助数据。
    """
    inputs = linked()
    assert migration_resolution_unit(inputs) == (".", "a")
    result = resolve_migration_conflicts(inputs, [choose(use="current")])
    assert result.merged == directory(
        {"a/doc": b"upstream", "state.db": b"current data", "b/doc": b"today"}
    )


def test_linked_incoming_uses_complete_unit_but_preserves_independent_identity_context() -> None:
    """
    incoming 完整保留数据库和链接，但导出中无关旧版 skill 不能进入候选写入。
    """
    inputs = linked()
    result = resolve_migration_conflicts(inputs, [choose(unit=(".", "a"), use="incoming")])
    assert result.merged == directory(
        {"a/doc": b"learned", "state.db": b"old data", "b/doc": b"today"},
        {"a/data": "../state.db"},
    )


@pytest.mark.parametrize(
    "values",
    [
        {"path": "a", "use": "incoming"},
        {"path": "a/doc", "use": "current"},
        {"unit": ("a",), "use": "current"},
        {"unit": ("b",), "use": "incoming"},
    ],
)
def test_linked_choices_cannot_split_or_switch_scope(values: dict[str, object]) -> None:
    """
    即使 source_conflict 的路径指向根，也不能把完整关联单元降成逐路径选择。

    :param values (dict[str, object]): 不完整或无关范围
    """
    with pytest.raises(ValueError, match="complete.*unit"):
        resolve_migration_conflicts(linked(), [choose(**values)])


def test_linked_plan_is_pending_without_choice_and_rejects_overlapping_whole_sides() -> None:
    """
    尚无选择不能输出任何部分目录，两个整体选择也不能依到达顺序覆盖。
    """
    inputs = linked()
    result = resolve_migration_conflicts(inputs, [])
    assert result.merged is None and result.conflicts == inputs.conflicts
    with pytest.raises(ValueError, match="overlap"):
        resolve_migration_conflicts(inputs, [choose(use="current"), choose(use="incoming")])


def test_reverse_directory_link_is_included_and_invalid_current_has_no_partial_tree() -> None:
    """
    只有账户目录存在的反向链接仍扩大范围，新版删除链接目标不能产生伪完整树。
    """
    inputs = ordinary()
    inputs = replace(
        inputs,
        directory=directory({"a/doc": b"old", "b/doc": b"keep"}, {"alias": "a/doc"}),
        current=directory({"a/new": b"upstream"}),
        conflicts=(SkillMergeConflict(path="a", reason="source_conflict", unit=(".", "a")),),
    )
    assert migration_resolution_unit(inputs) == (".", "a")
    result = resolve_migration_conflicts(inputs, [choose(use="current")])
    assert result.merged is None and result.conflicts[0].reason == "invalid_tree"
    repaired = directory({"a/new": b"upstream", "b/doc": b"keep"}, {"alias": "a/new"})
    result = resolve_migration_conflicts(inputs, [custom(repaired)])
    assert result.merged == repaired


def test_linked_current_does_not_use_stale_context_from_target_backing_tree() -> None:
    """
    目标 head 的完整历史树可能包含旧的其他来源，current 只采用目标自身内容。
    """
    inputs = linked()
    inputs = replace(
        inputs, current=directory({"a/doc": b"target own", "state.db": b"stale", "b/doc": b"stale"})
    )
    result = resolve_migration_conflicts(inputs, [choose(use="current")])
    assert result.merged == directory(
        {"a/doc": b"target own", "state.db": b"current data", "b/doc": b"today"}
    )


def test_custom_tree_can_include_exact_external_context_for_new_link() -> None:
    """
    人工目标新增到独立来源的链接时允许携带原样依赖，实际目录仍完整验证。
    """
    inputs = ordinary()
    tree = directory({"a/doc": b"resolved", "b/doc": b"independent"}, {"a/ref": "../b/doc"})
    result = resolve_migration_conflicts(inputs, [custom(tree)])
    assert result.merged == directory(
        {"a/doc": b"resolved", "b/doc": b"independent", "notes": b"aux"}, {"a/ref": "../b/doc"}
    )
    changed = directory({"a/doc": b"resolved", "b/doc": b"replaced"}, {"a/ref": "../b/doc"})
    with pytest.raises(ValueError, match="independent context"):
        resolve_migration_conflicts(inputs, [custom(changed)])


def test_linked_other_skill_is_selected_as_unit_and_unrelated_skill_is_preserved() -> None:
    """
    两个真实成员的关联必须整体计算，候选保留双方变化供授权层逐分支审核。
    """
    inputs = linked()
    inputs = replace(
        inputs,
        names=frozenset({"a", "b", "c"}),
        incoming=directory(
            {"a/doc": b"old", "b/doc": b"related", "c/doc": b"historical"}, {"a/ref": "../b/doc"}
        ),
        directory=directory({"a/doc": b"shown", "b/doc": b"today", "c/doc": b"independent"}),
        conflicts=(SkillMergeConflict(path="a", reason="source_conflict", unit=("a", "b")),),
    )
    assert migration_resolution_unit(inputs) == ("a", "b")
    incoming = resolve_migration_conflicts(inputs, [choose(use="incoming")])
    assert incoming.merged == directory(
        {"a/doc": b"old", "b/doc": b"related", "c/doc": b"independent"}, {"a/ref": "../b/doc"}
    )
    current = resolve_migration_conflicts(inputs, [choose(use="current")])
    assert current.merged == directory(
        {"a/doc": b"upstream", "b/doc": b"today", "c/doc": b"independent"}
    )


def test_opaque_independent_state_requires_complete_side() -> None:
    """
    没有外部链接的数据库分歧仍保持整项保护，不允许人工文件绕过。
    """
    base = directory({"a/state.db": b"old", "a/doc": b"old"})
    current = directory({"a/state.db": b"upstream", "a/doc": b"new"})
    incoming = directory({"a/state.db": b"learned", "a/doc": b"old"})
    inputs = replace(
        ordinary(),
        base=base,
        current=current,
        incoming=incoming,
        conflicts=merge_directory_manifests(base, current, incoming, {"a"}).conflicts,
    )
    assert inputs.conflicts[0].reason == "opaque_divergence"
    with pytest.raises(ValueError, match="complete unit"):
        resolve_migration_conflicts(inputs, [choose(path="a/state.db", use="incoming")])
    result = resolve_migration_conflicts(inputs, [choose(unit=("a",), use="incoming")])
    assert result.merged == directory(
        {"a/state.db": b"learned", "a/doc": b"old", "b/doc": b"independent", "notes": b"aux"}
    )


def test_whole_deletion_is_real_absence_and_other_sources_survive() -> None:
    """
    用户选择完整删除不伪造空文件，也不能删除无关账户状态。
    """
    inputs = ordinary()
    inputs = replace(
        inputs,
        incoming=SkillTreeManifest(),
        conflicts=(SkillMergeConflict(path="a", reason="changed_both", unit=("a",)),),
    )
    result = resolve_migration_conflicts(inputs, [choose(use="incoming")])
    assert result.merged == directory({"b/doc": b"independent", "notes": b"aux"})


def test_invalid_content_binding_and_outside_paths_are_rejected() -> None:
    """
    摘要、内容方法或路径范围不一致不能进入候选计算。
    """
    inputs = ordinary()
    with pytest.raises(ValueError, match="outside"):
        resolve_migration_conflicts(inputs, [choose(path="b/doc", use="current")])
    with pytest.raises(ValueError, match="method"):
        resolve_migration_conflicts(
            inputs, [LoadedResolutionChoice(SkillResolutionChoice(use="incoming"), inputs.incoming)]
        )
    wrong = LoadedResolutionChoice(
        SkillResolutionChoice(directory_tree_digest="0" * 64), inputs.current
    )
    with pytest.raises(ValueError, match="digest"):
        resolve_migration_conflicts(inputs, [wrong])
