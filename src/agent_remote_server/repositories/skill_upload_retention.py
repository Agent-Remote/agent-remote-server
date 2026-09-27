"""
为完整保活分析加载有计数校验的原上传摘要投影，不解码无关清单字段。
"""

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_storage import SkillContentUpload, SkillUploadObject


@dataclass(frozen=True)
class UploadObjectReference:
    """
    单用户原始暂存租约声明的内容身份，独立存在时不构成保活根。
    """

    upload_id: UUID
    digest: str


async def upload_references(
    session: AsyncSession, user_id: UUID, uploads: tuple[SkillContentUpload, ...], max_rows: int
) -> tuple[UploadObjectReference, ...]:
    """
    联合原始输入读取全部已索引暂存声明，缺失或额外行都拒绝完整分析。

    :param session (AsyncSession): 已取得用户内容锁的事务
    :param user_id (UUID): 只允许分析的用户
    :param uploads (tuple[SkillContentUpload, ...]): 同一事务中的完整上传元数据
    :param max_rows (int): 全索引尚可使用的行数预算
    :return tuple[UploadObjectReference, ...]: 不包含完整 JSON 的逐对象引用
    """
    expected = {
        upload.id: upload.object_index_count
        for upload in uploads
        if upload.status == "staged" and upload.object_index_version == 1
    }
    rows = await session.execute(
        select(SkillUploadObject.upload_id, SkillUploadObject.digest)
        .join(
            SkillContentUpload,
            (
                (SkillContentUpload.id == SkillUploadObject.upload_id)
                & (SkillContentUpload.user_id == SkillUploadObject.user_id)
                & (SkillContentUpload.tree_digest == SkillUploadObject.tree_digest)
                & (SkillContentUpload.scope == SkillUploadObject.scope)
            ),
        )
        .where(
            SkillUploadObject.user_id == user_id,
            SkillContentUpload.status == "staged",
            SkillContentUpload.object_index_version == 1,
        )
        .limit(max_rows + 1)
    )
    counts: dict[UUID, int] = dict.fromkeys(expected, 0)
    result: list[UploadObjectReference] = []
    for upload_id, digest in rows:
        if upload_id not in expected or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("invalid upload retention reference")
        result.append(UploadObjectReference(upload_id, digest))
        counts[upload_id] += 1
        if len(result) > max_rows:
            raise ValueError("retention index row limit exceeded")
    if counts != expected:
        raise ValueError("incomplete upload retention index")
    return tuple(result)
