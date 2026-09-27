"""
对完整清单进行明确范围的元数据比较，保留删除、权限和类型变化。
"""

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff


def manifest_difference(
    before: SkillTreeManifest, after: SkillTreeManifest, name: str | None
) -> list[SkillStatePathDiff]:
    """
    分支预览只比较自身路径，目录预览包含全部实际变化。

    :param before (SkillTreeManifest): 真实旧基线
    :param after (SkillTreeManifest): 完整结果
    :param name (str | None): 单项名称或完整目录
    :return list[SkillStatePathDiff]: 规范排序的完整元数据差异
    """
    left = {
        entry.path: entry
        for entry in before.entries
        if name is None or (entry.path == name or entry.path.startswith(name + "/"))
    }
    right = {
        entry.path: entry
        for entry in after.entries
        if name is None or (entry.path == name or entry.path.startswith(name + "/"))
    }
    return [
        SkillStatePathDiff(path=path, base=left.get(path), current=right.get(path))
        for path in sorted(left.keys() | right.keys(), key=str.encode)
        if left.get(path) != right.get(path)
    ]
