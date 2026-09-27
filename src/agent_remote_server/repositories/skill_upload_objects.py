"""
持久化原始上传的对象声明投影，查询始终限定用户与完整输入。
"""

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_storage import SkillContentUpload, SkillUploadObject
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry


class SkillUploadObjectRepository:
    """
    调用方必须持有原上传所有者的存储写锁并负责事务提交。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定已有事务。

        :param session (AsyncSession): 已授权的数据库事务
        """
        self._session = session

    async def build(self, upload: SkillContentUpload, entries: dict[str, SkillTreeEntry]) -> None:
        """
        分批插入完整声明后发布版本，异常由外层事务回滚全部投影。

        :param upload (SkillContentUpload): 原始未索引上传
        :param entries (dict[str, SkillTreeEntry]): 已完整验证的唯一文件声明
        """
        values = list(entries.values())
        for offset in range(0, len(values), 500):
            await self._session.execute(
                insert(SkillUploadObject),
                [
                    {
                        "user_id": upload.user_id,
                        "upload_id": upload.id,
                        "tree_digest": upload.tree_digest,
                        "scope": upload.scope,
                        "digest": entry.sha256,
                        "entry_json": entry.model_dump(mode="json"),
                    }
                    for entry in values[offset : offset + 500]
                ],
            )
        upload.object_index_count = len(entries)
        upload.object_index_version = 1
        await self._session.flush()

    async def entry(self, upload: SkillContentUpload, digest: str) -> dict[str, object] | None:
        """
        只读取原上传的一个对象声明，不读取完整清单。

        :param upload (SkillContentUpload): 已重新授权的原始上传
        :param digest (str): 请求的唯一文件摘要
        :return dict[str, object] | None: 单文件声明或不存在
        """
        return await self._session.scalar(
            select(SkillUploadObject.entry_json).where(
                SkillUploadObject.user_id == upload.user_id,
                SkillUploadObject.upload_id == upload.id,
                SkillUploadObject.tree_digest == upload.tree_digest,
                SkillUploadObject.scope == upload.scope,
                SkillUploadObject.digest == digest,
            )
        )

    async def clear(self, upload: SkillContentUpload) -> None:
        """
        终态转换只删除派生声明，保留原始上传身份及完整清单。

        :param upload (SkillContentUpload): 正在同事务转换为终态的上传
        """
        await self._session.execute(
            delete(SkillUploadObject).where(
                SkillUploadObject.user_id == upload.user_id,
                SkillUploadObject.upload_id == upload.id,
            )
        )
