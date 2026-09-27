"""
验证显式解决计划保留完整侧、不拆数据库单元且不发布不完整树。
"""

import pytest
from pydantic import ValidationError
from test_skill_directory_merge import directory

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.skill_manager.directory_merge import merge_directory_manifests
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.resolution import (
    LoadedResolutionChoice,
    resolve_directory_conflicts,
)


def test_partial_plan_never_returns_partial_publishable_tree() -> None:
    """
    两个冲突只选择一个时，只返回剩余冲突，不返回含占位值的部分树。
    """
    base = directory({"a/one": b"old", "a/two": b"old"})
    current = directory({"a/one": b"left", "a/two": b"left"})
    incoming = directory({"a/one": b"right", "a/two": b"right"})
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    first = LoadedResolutionChoice(SkillResolutionChoice(path="a/one", use="current"))
    pending = resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, [first])
    assert pending.merged is None and [item.path for item in pending.conflicts] == ["a/two"]
    second = LoadedResolutionChoice(SkillResolutionChoice(path="a/two", use="incoming"))
    done = resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, [first, second])
    assert done.merged == directory({"a/one": b"left", "a/two": b"right"})


def test_custom_file_keeps_other_uncontested_changes() -> None:
    """
    人工普通文件替换仅作用于冲突路径，其余单侧变更仍保留。
    """
    base = directory({"a/doc": b"old"})
    current = directory({"a/doc": b"left", "a/current": b"one"})
    incoming = directory({"a/doc": b"right", "a/incoming": b"two"})
    replacement = directory({"content": b"user resolved"})
    choice = LoadedResolutionChoice(
        SkillResolutionChoice(path="a/doc", file_tree_digest=manifest_digest(replacement)),
        replacement,
    )
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    result = resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, [choice])
    assert result.merged == directory(
        {"a/doc": b"user resolved", "a/current": b"one", "a/incoming": b"two"}
    )


@pytest.mark.parametrize("use", ["current", "incoming"])
def test_delete_modify_choice_preserves_complete_selected_subtree(use: str) -> None:
    """
    删除与修改选择完整一侧，不借空文件替换来伪造删除。

    :param use (str): 选择的完整侧
    """
    base = directory({"a/doc": b"old"})
    current = SkillTreeManifest()
    incoming = directory({"a/doc": b"new"})
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    selected = SkillResolutionChoice.model_validate({"use": use})
    result = resolve_directory_conflicts(
        base, current, incoming, {"a"}, conflicts, [LoadedResolutionChoice(selected)]
    )
    assert result.merged == (current if use == "current" else incoming)
    replacement = directory({"file": b"replacement"})
    with pytest.raises(ValueError, match="ordinary file"):
        resolve_directory_conflicts(
            base,
            current,
            incoming,
            {"a"},
            conflicts,
            [
                LoadedResolutionChoice(
                    SkillResolutionChoice(
                        path="a/doc", file_tree_digest=manifest_digest(replacement)
                    ),
                    replacement,
                )
            ],
        )


def test_linked_database_unit_cannot_be_split_but_other_skill_is_retained() -> None:
    """
    跨技能数据库组必须整体选择，同时保留无关技能的单侧更新。
    """
    links = {"b/database": "../a/state.db"}
    base = directory({"a/state.db": b"old\x00", "b/doc": b"old", "c/doc": b"old"}, links)
    current = directory({"a/state.db": b"new\x00", "b/doc": b"old", "c/doc": b"new"}, links)
    incoming = directory({"a/state.db": b"old\x00", "b/doc": b"new", "c/doc": b"old"}, links)
    conflicts = merge_directory_manifests(base, current, incoming, {"a", "b", "c"}).conflicts
    with pytest.raises(ValueError, match="complete unit"):
        resolve_directory_conflicts(
            base,
            current,
            incoming,
            {"a", "b", "c"},
            conflicts,
            [
                LoadedResolutionChoice(
                    SkillResolutionChoice(path="a/state.db", use="current"),
                )
            ],
        )
    with pytest.raises(ValueError, match="complete conflicting unit"):
        resolve_directory_conflicts(
            base,
            current,
            incoming,
            {"a", "b", "c"},
            conflicts,
            [
                LoadedResolutionChoice(
                    SkillResolutionChoice(unit=("a",), use="current"),
                )
            ],
        )
    result = resolve_directory_conflicts(
        base,
        current,
        incoming,
        {"a", "b", "c"},
        conflicts,
        [
            LoadedResolutionChoice(
                SkillResolutionChoice(unit=("a", "b"), use="incoming"),
            )
        ],
    )
    assert result.merged == directory(
        {"a/state.db": b"old\x00", "b/doc": b"new", "c/doc": b"new"}, links
    )


def test_new_link_cycle_after_choices_requires_full_repair() -> None:
    """
    分别有效的两侧链接组合成循环时，完整校验阻止发布。
    """
    base = directory({"a/one": b"old", "a/two": b"old"})
    current = directory({"a/two": b"left"}, {"a/one": "two"})
    incoming = directory({"a/one": b"right"}, {"a/two": "one"})
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    choices = [
        LoadedResolutionChoice(SkillResolutionChoice(path="a/one", use="current")),
        LoadedResolutionChoice(SkillResolutionChoice(path="a/two", use="incoming")),
    ]
    result = resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, choices)
    assert result.merged is None and result.conflicts[0].reason == "invalid_tree"
    fixed = directory({"a/one": b"safe", "a/two": b"safe"})
    repair = LoadedResolutionChoice(
        SkillResolutionChoice(directory_tree_digest=manifest_digest(fixed)), fixed
    )
    assert (
        resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, [repair]).merged
        == fixed
    )


def test_overlapping_plans_and_unknown_paths_are_rejected() -> None:
    """
    计划不能靠顺序决定重叠选择，也不能修改未发生冲突的路径。
    """
    base = directory({"a/doc": b"old"})
    current = directory({"a/doc": b"left"})
    incoming = directory({"a/doc": b"right"})
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    with pytest.raises(ValueError, match="overlap"):
        resolve_directory_conflicts(
            base,
            current,
            incoming,
            {"a"},
            conflicts,
            [
                LoadedResolutionChoice(SkillResolutionChoice(use="current")),
                LoadedResolutionChoice(SkillResolutionChoice(path="a/doc", use="incoming")),
            ],
        )
    with pytest.raises(ValueError, match="independently resolvable"):
        resolve_directory_conflicts(
            base,
            current,
            incoming,
            {"a"},
            conflicts,
            [
                LoadedResolutionChoice(SkillResolutionChoice(path="a/unknown", use="incoming")),
            ],
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"use": "current", "file_tree_digest": "a" * 64, "path": "a/doc"},
        {"file_tree_digest": "a" * 64},
        {"path": "../escape", "use": "incoming"},
        {"path": "a/doc", "directory_tree_digest": "a" * 64},
        {"unit": ["b", "a"], "use": "current"},
    ],
)
def test_resolution_contract_rejects_ambiguous_choices(payload: dict[str, object]) -> None:
    """
    严格契约拒绝路径逃逸、方式混用和不明确范围。

    :param payload (dict[str, object]): 非法选择请求
    """
    with pytest.raises(ValidationError):
        SkillResolutionChoice.model_validate(payload)


def test_path_incoming_restores_required_parent_without_resurrecting_other_files() -> None:
    """
    删除整个目录后选回已修改文件，只恢复必需父目录，不恢复未选中的旧兄弟文件。
    """
    base = directory({"a/doc": b"old", "a/other": b"old"})
    current = SkillTreeManifest()
    incoming = directory({"a/doc": b"new", "a/other": b"old"})
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    choice = LoadedResolutionChoice(SkillResolutionChoice(path="a/doc", use="incoming"))
    result = resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, [choice])
    assert result.merged == directory({"a/doc": b"new"})


def test_type_conflict_choice_replaces_entire_subtree() -> None:
    """
    目录替换成完整文件侧时，不能残留另一侧新增的目录后代。
    """
    base = directory({"a/value": b"old"})
    current = directory({"a/value/child": b"directory side"})
    incoming = directory({"a/value": b"file side"})
    conflicts = merge_directory_manifests(base, current, incoming, {"a"}).conflicts
    choice = LoadedResolutionChoice(SkillResolutionChoice(path="a/value", use="incoming"))
    result = resolve_directory_conflicts(base, current, incoming, {"a"}, conflicts, [choice])
    assert result.merged == incoming
