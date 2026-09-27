"""
按显式计划替换完整侧或文件，不把尚未解决的部分树暴露为发布结果。
"""

from dataclasses import dataclass

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict, SkillMergeResult
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.skill_manager.directory_merge import directory_merge_units
from agent_remote_server.skill_manager.manifest import manifest_digest


@dataclass(frozen=True)
class LoadedResolutionChoice:
    """
    人工树必须由调用方按用户授权完整加载，纯算法不读取对象存储。
    """

    choice: SkillResolutionChoice
    tree: SkillTreeManifest | None = None


def resolve_directory_conflicts(
    base: SkillTreeManifest,
    current: SkillTreeManifest,
    incoming: SkillTreeManifest,
    item_names: set[str],
    conflicts: tuple[SkillMergeConflict, ...],
    choices: list[LoadedResolutionChoice],
) -> SkillMergeResult:
    """
    覆盖全部原始冲突并验证完整目录后才提供可发布清单。

    :param base (SkillTreeManifest): 不可变共同基线
    :param current (SkillTreeManifest): 冲突时固定的当前比较树
    :param incoming (SkillTreeManifest): 完整原始提交
    :param item_names (set[str]): 授权层确定的稳定身份名称
    :param conflicts (tuple[SkillMergeConflict, ...]): 已保存的原始冲突
    :param choices (list[LoadedResolutionChoice]): 已授权并加载的完整计划
    :return SkillMergeResult: 完整结果或尚未解决的冲突
    """
    units = directory_merge_units((base, current, incoming), item_names)
    for index, loaded in enumerate(choices):
        _validate_choice(loaded, current, incoming, conflicts, units)
        if any(
            choices_overlap(loaded.choice, other.choice, item_names) for other in choices[:index]
        ):
            raise ValueError("resolution choices overlap")
    remaining = tuple(
        conflict
        for conflict in conflicts
        if not any(_covers(loaded.choice, conflict, item_names) for loaded in choices)
    )
    if remaining:
        return SkillMergeResult(conflicts=remaining)
    original = {entry.path: entry for entry in base.entries}
    left = {entry.path: entry for entry in current.entries}
    right = {entry.path: entry for entry in incoming.entries}
    entries: dict[str, SkillTreeEntry] = {}
    for path in original.keys() | left.keys() | right.keys():
        old, ours, theirs = original.get(path), left.get(path), right.get(path)
        selected = theirs if ours == old else ours
        if ours == theirs or theirs == old:
            selected = ours
        if selected is not None:
            entries[path] = selected
    for loaded in choices:
        choice = loaded.choice
        source = (
            loaded.tree
            if choice.tree_digest is not None
            else (current if choice.use == "current" else incoming)
        )
        assert source is not None
        entries = {
            path: entry
            for path, entry in entries.items()
            if not _in_scope(choice, path, item_names)
        }
        if choice.file_tree_digest is not None:
            assert choice.path is not None
            replacement = source.entries[0].model_copy(update={"path": choice.path})
            entries[choice.path] = replacement
        else:
            entries.update(
                (entry.path, entry)
                for entry in source.entries
                if _in_scope(choice, entry.path, item_names)
            )
            if choice.path is not None and choice.path in entries:
                source_entries = {entry.path: entry for entry in source.entries}
                parts = choice.path.split("/")
                for depth in range(1, len(parts)):
                    ancestor = "/".join(parts[:depth])
                    parent = source_entries.get(ancestor)
                    if parent is not None and parent.kind == "directory":
                        entries.setdefault(ancestor, parent)
    try:
        return SkillMergeResult(
            merged=SkillTreeManifest(
                entries=tuple(sorted(entries.values(), key=lambda entry: entry.path.encode()))
            )
        )
    except ValueError:
        return SkillMergeResult(
            conflicts=(
                SkillMergeConflict(
                    path=".",
                    reason="invalid_tree",
                    unit=tuple(sorted(item_names | {"."})),
                ),
            )
        )


def choices_overlap(
    left: SkillResolutionChoice, right: SkillResolutionChoice, names: set[str]
) -> bool:
    """
    完整侧选择不能与其中的路径或另一个重叠范围同时生效。

    :param left (SkillResolutionChoice): 一次选择
    :param right (SkillResolutionChoice): 另一次选择
    :param names (set[str]): 稳定成员名称
    :return bool: 范围是否相交
    """
    if left.whole or right.whole:
        return True
    if left.path is not None:
        if right.path is not None:
            return _descendant(left.path, right.path) or _descendant(right.path, left.path)
        return _in_scope(right, left.path, names)
    if right.path is not None:
        return _in_scope(left, right.path, names)
    return bool(set(left.unit) & set(right.unit))


def _validate_choice(
    loaded: LoadedResolutionChoice,
    current: SkillTreeManifest,
    incoming: SkillTreeManifest,
    conflicts: tuple[SkillMergeConflict, ...],
    units: tuple[tuple[str, ...], ...],
) -> None:
    """
    禁止文件级拆分不透明组、给结构冲突上传文件或选择无关路径。

    :param loaded (LoadedResolutionChoice): 用户明确选择及人工内容
    :param current (SkillTreeManifest): 当前完整侧
    :param incoming (SkillTreeManifest): 输入完整侧
    :param conflicts (tuple[SkillMergeConflict, ...]): 待解决原始冲突
    :param units (tuple[tuple[str, ...], ...]): 三份输入形成的完整连通单元
    """
    choice = loaded.choice
    if (choice.tree_digest is None) != (loaded.tree is None):
        raise ValueError("resolution content does not match its declared method")
    if loaded.tree is not None and manifest_digest(loaded.tree) != choice.tree_digest:
        raise ValueError("loaded resolution content differs from its digest")
    if choice.unit and (
        choice.unit not in units
        or not any(
            conflict.unit and set(conflict.unit) <= set(choice.unit) for conflict in conflicts
        )
    ):
        raise ValueError("choice does not select a complete conflicting unit")
    if choice.path is None:
        return
    scopes = {name for unit in units for name in unit}
    root = choice.path.split("/", 1)[0]
    scope = root if root in scopes else "."
    if any(
        conflict.reason == "opaque_divergence" and scope in conflict.unit for conflict in conflicts
    ):
        raise ValueError("opaque state requires a complete unit choice")
    matches = [conflict for conflict in conflicts if conflict.path == choice.path]
    if not matches or any(
        conflict.reason in {"opaque_divergence", "invalid_tree"} for conflict in matches
    ):
        raise ValueError("path is not an independently resolvable conflict")
    if choice.file_tree_digest is not None:
        if any(conflict.reason == "source_conflict" for conflict in matches):
            raise ValueError("a file cannot resolve source identity")
        before = next((entry for entry in current.entries if entry.path == choice.path), None)
        after = next((entry for entry in incoming.entries if entry.path == choice.path), None)
        if before is None or after is None or before.kind != "file" or after.kind != "file":
            raise ValueError("file choice is only valid for ordinary file content conflicts")
        if (
            loaded.tree is None
            or len(loaded.tree.entries) != 1
            or loaded.tree.entries[0].kind != "file"
        ):
            raise ValueError("file choice requires exactly one ordinary file")


def _covers(choice: SkillResolutionChoice, conflict: SkillMergeConflict, names: set[str]) -> bool:
    """
    一个完整结构侧可以同时解决其全部子路径冲突。

    :param choice (SkillResolutionChoice): 已验证选择
    :param conflict (SkillMergeConflict): 原始冲突
    :param names (set[str]): 稳定成员名称
    :return bool: 是否完整覆盖该冲突
    """
    if choice.whole:
        return True
    if choice.unit:
        return bool(conflict.unit) and set(conflict.unit) <= set(choice.unit)
    if conflict.reason in {"opaque_divergence", "invalid_tree"}:
        return False
    return _in_scope(choice, conflict.path, names)


def _in_scope(choice: SkillResolutionChoice, path: str, names: set[str]) -> bool:
    """
    根辅助范围是所有未具备显式成员身份的顶层名称集合。

    :param choice (SkillResolutionChoice): 明确选择
    :param path (str): 清单内路径
    :param names (set[str]): 稳定成员名称
    :return bool: 路径是否属于该完整替换范围
    """
    if choice.whole:
        return True
    if choice.path is not None:
        return _descendant(path, choice.path)
    root = path.split("/", 1)[0]
    return (root if root in names else ".") in choice.unit


def _descendant(path: str, parent: str) -> bool:
    """
    使用路径组件边界判断同一路径或真正后代。

    :param path (str): 待判断路径
    :param parent (str): 明确父范围
    :return bool: 是否属于完整子树
    """
    return path == parent or path.startswith(parent + "/")
