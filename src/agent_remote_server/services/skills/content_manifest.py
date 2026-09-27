"""
验证同一内容摘要在完整清单中的唯一字节声明。
"""

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content_errors import SkillContentError


def unique_files(manifest: SkillTreeManifest) -> dict[str, SkillTreeEntry]:
    """
    在预留空间前拒绝同摘要不同大小或分类的自相矛盾声明。

    :param manifest (SkillTreeManifest): 完整清单
    :return dict[str, SkillTreeEntry]: 按内容摘要去重的文件
    """
    entries: dict[str, SkillTreeEntry] = {}
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        previous = entries.get(entry.sha256)
        if previous is not None and (previous.size, previous.content_kind) != (
            entry.size,
            entry.content_kind,
        ):
            raise SkillContentError(
                "CONTENT_METADATA_CONFLICT", "one digest has inconsistent metadata"
            )
        entries[entry.sha256] = entry
    return entries
