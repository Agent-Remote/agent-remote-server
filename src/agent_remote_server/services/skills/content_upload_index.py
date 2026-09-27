"""
从原始不可变清单建立逐对象声明，避免每个文件重新解析整棵树。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.repositories.skill_upload_objects import SkillUploadObjectRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.content_manifest import unique_files
from agent_remote_server.skill_manager.manifest import manifest_digest


def validated_upload_manifest(upload: SkillContentUpload) -> SkillTreeManifest:
    """
    完整清单必须仍符合最初的内容身份，损坏不能产生新的写入权限。

    :param upload (SkillContentUpload): 已加载完整清单的原始上传
    :return SkillTreeManifest: 已校验的原始清单
    """
    try:
        manifest = SkillTreeManifest.model_validate(upload.manifest_json)
        if manifest_digest(manifest) != upload.tree_digest:
            raise ValueError("original upload digest changed")
    except ValueError as error:
        raise SkillContentError("CONTENT_INVALID", "original upload manifest is invalid") from error
    return manifest


class SkillUploadDeclarations:
    """
    在用户写锁内构建兼容索引并验证选定对象，不管理租约或提交事务。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        使用与原始上传相同的事务。

        :param session (AsyncSession): 外层持有存储用户锁的事务
        """
        self._storage = SkillStorageRepository(session)
        self.repository = SkillUploadObjectRepository(session)

    async def entry(self, upload: SkillContentUpload, digest: str) -> SkillTreeEntry:
        """
        旧上传完整回填一次，新上传只查询并验证一个原始声明。

        :param upload (SkillContentUpload): 已验证仍然有效的当前上传
        :param digest (str): 原始清单中的文件摘要
        :return SkillTreeEntry: 可以接收字节的精确文件声明
        """
        if upload.object_index_version == 0:
            original = await self._storage.upload(upload.user_id, upload.id)
            if original is None:
                raise SkillContentError("UPLOAD_NOT_FOUND", "upload not found")
            manifest = validated_upload_manifest(original)
            await self.repository.build(original, unique_files(manifest))
        elif upload.object_index_version != 1:
            raise SkillContentError("CONTENT_INVALID", "unknown upload declaration version")
        raw = await self.repository.entry(upload, digest)
        if raw is None:
            raise SkillContentError("CONTENT_NOT_DECLARED", "file is absent from upload manifest")
        try:
            entry = SkillTreeEntry.model_validate(raw)
            if entry.kind != "file" or entry.sha256 != digest:
                raise ValueError("indexed object identity differs")
        except ValueError as error:
            raise SkillContentError("CONTENT_INVALID", "upload declaration is invalid") from error
        return entry
