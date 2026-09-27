"""
按迁移的真实目标侧和完整目录上下文计算候选，关联单元不能被逐文件拆开。
"""

from dataclasses import dataclass

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict, SkillMergeResult
from agent_remote_server.skill_manager.directory_merge import directory_merge_units
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.resolution import (
    LoadedResolutionChoice,
    resolve_directory_conflicts,
)


@dataclass(frozen=True)
class MigrationResolutionInputs:
    """
    名称集合由授权层提供，额外导出根不自动成为可修改来源。
    """

    name: str
    names: frozenset[str]
    base: SkillTreeManifest
    current: SkillTreeManifest
    incoming: SkillTreeManifest
    directory: SkillTreeManifest
    conflicts: tuple[SkillMergeConflict, ...]


def migration_resolution_unit(inputs: MigrationResolutionInputs) -> tuple[str, ...]:
    """
    将账户目录反向链接纳入同一完整解决范围，不能只看迁移三侧。

    :param inputs (MigrationResolutionInputs): 已授权保存输入
    :return tuple[str, ...]: 目标所属完整关联单元
    """
    if inputs.name not in inputs.names or inputs.name == "." or "/" in inputs.name:
        raise ValueError("migration target must be one authorized top-level identity")
    units = directory_merge_units(
        (inputs.base, inputs.current, inputs.incoming, inputs.directory), set(inputs.names)
    )
    return next((unit for unit in units if inputs.name in unit), (inputs.name,))


def resolve_migration_conflicts(
    inputs: MigrationResolutionInputs, choices: list[LoadedResolutionChoice]
) -> SkillMergeResult:
    """
    完整有效候选仍需上层核对来源身份和事务前置条件，不能直接作为发布授权。

    :param inputs (MigrationResolutionInputs): 保存的四份输入和真实名称
    :param choices (list[LoadedResolutionChoice]): 已按本迁移授权并验证的选择
    :return SkillMergeResult: 完整目录候选或仍需处理的冲突
    """
    if not inputs.conflicts:
        raise ValueError("migration resolution requires a saved conflict")
    unit = migration_resolution_unit(inputs)
    for loaded in choices:
        _validate_scope(inputs, loaded, unit)
    if len(unit) > 1:
        return _linked(inputs, choices, unit)
    base, current, incoming = (
        _member(tree, inputs.name) for tree in (inputs.base, inputs.current, inputs.incoming)
    )
    result = resolve_directory_conflicts(
        base, current, incoming, {inputs.name}, inputs.conflicts, choices
    )
    if result.merged is None:
        return result
    selected = tuple(
        entry for entry in result.merged.entries if _member_path(entry.path, inputs.name)
    )
    return _complete(inputs, selected, unit)


def _validate_scope(
    inputs: MigrationResolutionInputs, loaded: LoadedResolutionChoice, unit: tuple[str, ...]
) -> None:
    """
    完整目录人工选择可带原样上下文，但不能借它修改独立来源。

    :param inputs (MigrationResolutionInputs): 保存输入
    :param loaded (LoadedResolutionChoice): 已授权单次选择
    :param unit (tuple[str, ...]): 完整目标关联范围
    """
    choice = loaded.choice
    if (choice.tree_digest is None) != (loaded.tree is None):
        raise ValueError("resolution content does not match its method")
    if loaded.tree is not None and manifest_digest(loaded.tree) != choice.tree_digest:
        raise ValueError("resolution content differs from declared digest")
    if choice.unit and choice.unit != unit:
        raise ValueError("migration choice requires the complete target unit")
    if choice.path is not None and not _member_path(choice.path, inputs.name):
        raise ValueError("migration path is outside the target")
    if len(unit) > 1 and choice.path is not None:
        raise ValueError("linked migration state requires a complete unit choice")
    if choice.directory_tree_digest is not None:
        assert loaded.tree is not None
        directory = {entry.path: entry for entry in inputs.directory.entries}
        if any(
            not _in_unit(entry.path, unit, inputs.names) and directory.get(entry.path) != entry
            for entry in loaded.tree.entries
        ):
            raise ValueError("custom migration tree changes an independent context source")


def _linked(
    inputs: MigrationResolutionInputs,
    choices: list[LoadedResolutionChoice],
    unit: tuple[str, ...],
) -> SkillMergeResult:
    """
    关联迁移只能明确选择完整侧，当前侧的关联上下文取保存账户目录。

    :param inputs (MigrationResolutionInputs): 已授权保存输入
    :param choices (list[LoadedResolutionChoice]): 完整侧选择或人工目录
    :param unit (tuple[str, ...]): 目标完整关联范围
    :return SkillMergeResult: 完整候选或未解决的关联冲突
    """
    if not choices:
        return SkillMergeResult(conflicts=inputs.conflicts)
    if len(choices) != 1:
        raise ValueError("migration choices overlap on a linked unit")
    loaded = choices[0]
    choice = loaded.choice
    if choice.use == "current":
        selected = tuple(
            entry for entry in inputs.current.entries if _member_path(entry.path, inputs.name)
        ) + tuple(
            entry
            for entry in inputs.directory.entries
            if _in_unit(entry.path, unit, inputs.names)
            and not _member_path(entry.path, inputs.name)
        )
    else:
        source = loaded.tree if loaded.tree is not None else inputs.incoming
        selected = tuple(
            entry for entry in source.entries if _in_unit(entry.path, unit, inputs.names)
        )
    return _complete(inputs, selected, unit)


def _complete(
    inputs: MigrationResolutionInputs,
    selected: tuple[SkillTreeEntry, ...],
    unit: tuple[str, ...],
) -> SkillMergeResult:
    """
    只替换目标范围并验证最终完整目录，悬空链接或类型错误不输出部分结果。

    :param inputs (MigrationResolutionInputs): 保存输入
    :param selected (tuple[SkillTreeEntry, ...]): 已选范围条目
    :param unit (tuple[str, ...]): 允许替换的完整单元
    :return SkillMergeResult: 完整目录或结构冲突
    """
    entries = (
        tuple(
            entry
            for entry in inputs.directory.entries
            if not _in_unit(entry.path, unit, inputs.names)
        )
        + selected
    )
    try:
        return SkillMergeResult(
            merged=SkillTreeManifest(
                entries=tuple(sorted(entries, key=lambda entry: entry.path.encode()))
            )
        )
    except ValueError:
        return SkillMergeResult(
            conflicts=(SkillMergeConflict(path=inputs.name, reason="invalid_tree", unit=unit),)
        )


def _member(tree: SkillTreeManifest, name: str) -> SkillTreeManifest:
    """
    仅在四侧已证明独立后提取成员，保留路径和原始权限。

    :param tree (SkillTreeManifest): 原始完整树
    :param name (str): 授权目标名
    :return SkillTreeManifest: 独立目标比较树
    """
    return SkillTreeManifest(
        entries=tuple(entry for entry in tree.entries if _member_path(entry.path, name))
    )


def _member_path(path: str, name: str) -> bool:
    """
    按组件边界匹配成员，避免同名前缀混入。

    :param path (str): 原始相对路径
    :param name (str): 目标根名
    :return bool: 是否属于目标成员
    """
    return path == name or path.startswith(name + "/")


def _in_unit(path: str, unit: tuple[str, ...], names: frozenset[str]) -> bool:
    """
    辅助根统一属于显式点范围，稳定成员由授权层名称集合决定。

    :param path (str): 原始相对路径
    :param unit (tuple[str, ...]): 完整关联单元
    :param names (frozenset[str]): 已授权稳定来源名称
    :return bool: 是否属于选中范围
    """
    root = path.split("/", 1)[0]
    return (root if root in names else ".") in unit
