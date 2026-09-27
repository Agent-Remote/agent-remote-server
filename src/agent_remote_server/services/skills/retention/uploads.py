"""
在保活图和删除预测中共享同一有效上传声明解释，避免遗漏零预留租约。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


def active_uploads(index: RetentionIndex, now: datetime) -> dict[UUID, SkillContentUpload]:
    """
    仅租约阶段和时间决定活跃状态，是否新增预留字节不能释放上传保护。

    :param index (RetentionIndex): 已完整验证的用户库存
    :param now (datetime): 本次分析固定时间
    :return dict[UUID, SkillContentUpload]: 当前仍有文件消费者的原始上传
    """
    result: dict[UUID, SkillContentUpload] = {}
    for upload in index.uploads:
        expires = upload.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if upload.status == "staged" and expires > now:
            result[upload.id] = upload
    return result


def upload_file_references(
    index: RetentionIndex, uploads: dict[UUID, SkillContentUpload]
) -> Iterator[tuple[UUID, str]]:
    """
    新版使用计数已核对的唯一摘要，旧版继续完整解析清单后按文件身份去重。

    :param index (RetentionIndex): 已完整验证的用户库存
    :param uploads (dict[UUID, SkillContentUpload]): 本次仍然有效的上传集合
    :return Iterator[tuple[UUID, str]]: 每个有效上传声明的唯一文件摘要
    """
    for upload in uploads.values():
        if upload.object_index_version == 0:
            manifest = SkillTreeManifest.model_validate(upload.manifest_json)
            for digest in {entry.sha256 for entry in manifest.entries if entry.kind == "file"}:
                yield upload.id, digest
    for reference in index.upload_objects:
        if reference.upload_id in uploads:
            yield reference.upload_id, reference.digest
