"""
保守合并技能目录树并保护不透明运行数据。
"""

from pydantic import ValidationError

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_merge import SkillMergeConflict, SkillMergeResult


def merge_manifests(
    base: SkillTreeManifest,
    current: SkillTreeManifest,
    incoming: SkillTreeManifest,
) -> SkillMergeResult:
    """
    以共同基线比较双方变化且只返回完整有效结果。

    :param base (SkillTreeManifest): 共同基线清单
    :param current (SkillTreeManifest): 当前已发布清单
    :param incoming (SkillTreeManifest): 本次提交清单
    :return SkillMergeResult: 完整合并结果或保留双方的冲突描述
    """
    if current == incoming or incoming == base:
        return SkillMergeResult(merged=current)
    if current == base:
        return SkillMergeResult(merged=incoming)
    original = {entry.path: entry for entry in base.entries}
    left = {entry.path: entry for entry in current.entries}
    right = {entry.path: entry for entry in incoming.entries}
    if _opaque_change(original, left) or _opaque_change(original, right):
        return SkillMergeResult(
            conflicts=(SkillMergeConflict(path=".", reason="opaque_divergence"),)
        )
    merged: list[SkillTreeEntry] = []
    conflicts: list[SkillMergeConflict] = []
    for path in sorted(original.keys() | left.keys() | right.keys(), key=str.encode):
        old = original.get(path)
        ours = left.get(path)
        theirs = right.get(path)
        if ours == theirs or theirs == old:
            selected = ours
        elif ours == old:
            selected = theirs
        else:
            conflicts.append(SkillMergeConflict(path=path, reason="changed_both"))
            continue
        if selected is not None:
            merged.append(selected)
    if conflicts:
        return SkillMergeResult(conflicts=tuple(conflicts))
    try:
        manifest = SkillTreeManifest(entries=tuple(merged))
    except ValidationError:
        return SkillMergeResult(conflicts=(SkillMergeConflict(path=".", reason="invalid_tree"),))
    return SkillMergeResult(merged=manifest)


def _opaque_change(base: dict[str, SkillTreeEntry], branch: dict[str, SkillTreeEntry]) -> bool:
    """
    检查分支变更是否涉及二进制或数据库状态。

    :param base (dict[str, SkillTreeEntry]): 基线路径索引
    :param branch (dict[str, SkillTreeEntry]): 分支路径索引
    :return bool: 是否必须使用整个技能作为合并单元
    """
    for path in base.keys() | branch.keys():
        before = base.get(path)
        after = branch.get(path)
        if before == after:
            continue
        for entry in (before, after):
            if entry is not None and entry.kind == "file":
                name = entry.path.rsplit("/", 1)[-1].lower()
                if entry.content_kind == "binary" or name.endswith(
                    (".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal")
                ):
                    return True
    return False
