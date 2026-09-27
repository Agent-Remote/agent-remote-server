"""
为首次准备和显式增量迁移共享原始包、检查点及实际内容校验。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class MigrationContent:
    """
    只读取同所有者完整内容，缺失或过期数据不得变成空基线。
    """

    queries: SkillStateQueryService
    store: PrivateObjectStore

    async def original(
        self, user_id: UUID, installation_id: UUID, revision_id: UUID, name: str
    ) -> tuple[SkillTreeManifest, int]:
        """
        原始包加账户前缀后仍是独立完整树，登记顺序不代表语义兼容性。

        :param user_id (UUID): 内容所有者
        :param installation_id (UUID): 稳定安装身份
        :param revision_id (UUID): 不可变原始版本
        :param name (str): 账户目录名称
        :return tuple[SkillTreeManifest, int]: 完整带前缀原始树与登记编号
        """
        revision = await self.queries.library.revision(user_id, installation_id, str(revision_id))
        if revision is None or not revision.retained or revision.tree_digest is None:
            raise SkillContentError(
                "REVISION_EXPIRED", "migration original package is not retained"
            )
        package = await self.queries.content.read_tree(user_id, "package", revision.tree_digest)
        return SkillTreeManifest(
            entries=(
                SkillTreeEntry(path=name, kind="directory", mode=0o755),
                *(
                    entry.model_copy(update={"path": name + "/" + entry.path})
                    for entry in package.entries
                ),
            )
        ), revision.number

    async def tree(self, checkpoint: SkillCheckpoint) -> SkillTreeManifest:
        """
        读取保留的完整原始引用，不能将过期内容当作空树。

        :param checkpoint (SkillCheckpoint): 同账户授权检查点
        :return SkillTreeManifest: 完整不可变树
        """
        if not checkpoint.retained or checkpoint.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "migration checkpoint is not retained")
        return await self.queries.content.read_tree(
            checkpoint.user_id, "state", checkpoint.tree_digest
        )

    async def verify(self, user_id: UUID, tree: SkillTreeManifest) -> None:
        """
        实际字节验证同样应用于预览，避免发布仅有摘要的混合树。

        :param user_id (UUID): 内容所有者
        :param tree (SkillTreeManifest): 完整待验证树
        """
        try:
            await self.store.verify_manifest(user_id, tree)
        except FileNotFoundError as error:
            raise SkillContentError(
                "CONTENT_INCOMPLETE", "migration content is unavailable"
            ) from error
        except ValueError as error:
            raise SkillContentError(
                "CONTENT_INVALID", "migration content verification failed"
            ) from error
