"""
根据真实相对链接连接账户技能和根级数据，原子合并完整目录。
"""

from agent_remote_server.schemas.skill_manifest import (
    SkillTreeEntry,
    SkillTreeManifest,
    resolve_manifest_link,
    validate_relative_path,
)
from agent_remote_server.schemas.skill_merge import SkillDirectoryMergeResult, SkillMergeConflict
from agent_remote_server.skill_manager.merge import merge_manifests


class _ConnectedScopes:
    """
    使用迭代路径压缩，避免长链接关系链触发 Python 递归限制。
    """

    def __init__(self, scopes: set[str]) -> None:
        """
        为完整树中实际出现的范围建立独立根。

        :param scopes (set[str]): 已确定归属的范围名称
        """
        self._parents = {scope: scope for scope in scopes}

    def root(self, scope: str) -> str:
        """
        取得并压缩该范围所属连通组。

        :param scope (str): 已登记范围
        :return str: 当前组根
        """
        root = scope
        while self._parents[root] != root:
            root = self._parents[root]
        while scope != root:
            parent = self._parents[scope]
            self._parents[scope] = root
            scope = parent
        return root

    def connect(self, left: str, right: str) -> None:
        """
        相对链接的源和每个真实依赖必须属于同一合并单元。

        :param left (str): 链接源范围
        :param right (str): 解析所依赖的范围
        """
        a, b = self.root(left), self.root(right)
        if a != b:
            self._parents[max(a, b)] = min(a, b)

    def units(self) -> tuple[tuple[str, ...], ...]:
        """
        输出与输入遍历顺序无关的规范连通单元。

        :return tuple[tuple[str, ...], ...]: 规范排序的范围组
        """
        groups: dict[str, list[str]] = {}
        for scope in self._parents:
            groups.setdefault(self.root(scope), []).append(scope)
        ordered = [tuple(sorted(group, key=str.encode)) for group in groups.values()]
        return tuple(sorted(ordered, key=lambda group: tuple(name.encode() for name in group)))


def merge_directory_manifests(
    base: SkillTreeManifest,
    current: SkillTreeManifest,
    incoming: SkillTreeManifest,
    item_names: set[str],
) -> SkillDirectoryMergeResult:
    """
    每个关联单元独立执行不透明保护，任一冲突都不返回部分可发布树。

    :param base (SkillTreeManifest): 会话完整共同基线
    :param current (SkillTreeManifest): 同身份分支组成的当前完整树
    :param incoming (SkillTreeManifest): 已完整持久化的输入树
    :param item_names (set[str]): 调用方已确认稳定身份的技能名称
    :return SkillDirectoryMergeResult: 完整合并结果及共同解决范围
    """
    manifests = (base, current, incoming)
    units = directory_merge_units(manifests, item_names)
    unit_for_scope = {name: index for index, unit in enumerate(units) for name in unit}
    partitions: list[list[list[SkillTreeEntry]]] = []
    for manifest in manifests:
        groups: list[list[SkillTreeEntry]] = [[] for _ in units]
        for entry in manifest.entries:
            groups[unit_for_scope[_scope(entry.path, item_names)]].append(entry)
        partitions.append(groups)
    merged: list[SkillTreeEntry] = []
    conflicts: list[SkillMergeConflict] = []
    for index, unit in enumerate(units):
        trees = [SkillTreeManifest(entries=tuple(parts[index])) for parts in partitions]
        result = merge_manifests(trees[0], trees[1], trees[2])
        if result.merged is not None:
            merged.extend(result.merged.entries)
        conflicts.extend(
            conflict.model_copy(update={"unit": unit}) for conflict in result.conflicts
        )
    if conflicts:
        return SkillDirectoryMergeResult(units=units, conflicts=tuple(conflicts))
    complete = SkillTreeManifest(
        entries=tuple(sorted(merged, key=lambda entry: entry.path.encode()))
    )
    return SkillDirectoryMergeResult(units=units, merged=complete)


def directory_merge_units(
    manifests: tuple[SkillTreeManifest, ...], item_names: set[str]
) -> tuple[tuple[str, ...], ...]:
    """
    连接全部输入中的直接和间接相对链接，不把运行依赖链接当作内容读取。

    :param manifests (tuple[SkillTreeManifest, ...]): 全部已验证完整输入
    :param item_names (set[str]): 已确认稳定身份的技能名称
    :return tuple[tuple[str, ...], ...]: 不可拆分的共同合并范围
    """
    for name in item_names:
        validate_relative_path(name)
        if "/" in name:
            raise ValueError("skill merge member must be a top-level name")
    scopes = {
        _scope(entry.path, item_names) for manifest in manifests for entry in manifest.entries
    }
    connected = _ConnectedScopes(scopes)
    for manifest in manifests:
        entries = {entry.path: entry for entry in manifest.entries}
        for entry in manifest.entries:
            if entry.kind != "symlink":
                continue
            _, dependencies = resolve_manifest_link(entry, entries)
            source = _scope(entry.path, item_names)
            for dependency in dependencies:
                connected.connect(source, _scope(dependency, item_names))
    return connected.units()


def _scope(path: str, item_names: set[str]) -> str:
    """
    只有明确传入的身份可独立计量，其余所有路径合为根级辅助单元。

    :param path (str): 已验证清单路径
    :param item_names (set[str]): 已确认技能名称
    :return str: 技能名称或表示辅助范围的点
    """
    name = path.split("/", 1)[0]
    return name if name in item_names else "."
