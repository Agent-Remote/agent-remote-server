"""
登记新发现技能的账户本地候选，保留链接依赖且不激活任何 head。
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_local import AccountLocalSkill, AccountLocalSkillRevision
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_library import validate_skill_name
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class LocalSkillCandidateService:
    """
    内部候选登记入口，激活留给持有发布前置条件的事务。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享内容引用和账户锁所在事务。

        :param session (AsyncSession): 外层数据库事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 部署额度
        """
        self._session = session
        self._library = SkillLibraryRepository(session)
        self._local = SkillLocalRepository(session)
        self._runtime = SkillRuntimeRepository(session)
        self._content = SkillContentService(session, store, policy)
        self._store = store
        self._policy = policy

    async def register(
        self, user_id: UUID, account_id: UUID, checkpoint_id: UUID, name: str
    ) -> AccountLocalSkill:
        """
        校验实际内容后幂等保存源身份，同名不同提交仍是独立候选。

        :param user_id (UUID): 已认证用户
        :param account_id (UUID): 目标账户
        :param checkpoint_id (UUID): 已完整持久化的源目录检查点
        :param name (str): 目标顶层目录名称
        :return AccountLocalSkill: 未自动激活的本地来源
        """
        async with retention_mutation(self._session, user_id):
            try:
                validate_skill_name(name)
            except ValueError as error:
                raise SkillContentError("INVALID_SKILL_NAME", str(error)) from error
            await self._library.lock_library(user_id)
            checkpoint = await self._runtime.checkpoint(user_id, account_id, checkpoint_id)
            if checkpoint is None or checkpoint.scope != "directory":
                raise SkillContentError("CHECKPOINT_NOT_FOUND", "source directory not found")
            if not checkpoint.retained or checkpoint.tree_digest is None:
                raise SkillContentError("STATE_EXPIRED", "source directory has expired")
            previous = await self._local.candidate(user_id, account_id, checkpoint_id, name)
            if previous is not None:
                return previous
            manifest = await self._content.read_tree(user_id, "state", checkpoint.tree_digest)
            root = next((entry for entry in manifest.entries if entry.path == name), None)
            if root is None or root.kind != "directory":
                raise SkillContentError("INVALID_SKILL_FORMAT", "skill root must be a directory")
            if (
                sum(
                    entry.size
                    for entry in manifest.entries
                    if entry.path == name or entry.path.startswith(name + "/")
                )
                > self._policy.checkpoint_bytes
            ):
                raise SkillContentError("QUOTA_EXCEEDED", "local skill exceeds its quota")
            await self._store.verify_manifest(user_id, manifest)
            entry = _skill_document(manifest, name + "/SKILL.md")
            metadata = _metadata(await self._store.read_prefix(user_id, entry, 65_544), name)
            item = AccountLocalSkill(
                id=uuid4(),
                user_id=user_id,
                account_id=account_id,
                name=name,
                source_checkpoint_id=checkpoint_id,
                status="staged",
                enabled=True,
            )
            self._runtime.add(item)
            await self._runtime.flush()
            revision = AccountLocalSkillRevision(
                id=uuid4(),
                user_id=user_id,
                account_id=account_id,
                local_skill_id=item.id,
                number=1,
                tree_digest=checkpoint.tree_digest,
                content_digest=checkpoint.tree_digest,
                subtree_prefix=name,
                metadata_json=metadata,
            )
            self._runtime.add(revision)
            await self._runtime.flush()
            item.default_revision_id = revision.id
            await self._runtime.flush()
            return item
