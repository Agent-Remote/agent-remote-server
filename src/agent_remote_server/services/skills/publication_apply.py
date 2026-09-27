"""
把已完整合并的目录、分支和新本地身份一次提交到权威 head。
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.services.skills.publication_context import (
    PublicationBranch,
    PublicationContext,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class PublicationApply:
    """
    所有内容已经齐备；任何 CAS 失败均要求调用方回滚整个事务。
    """

    context: PublicationContext
    store: PrivateObjectStore

    async def publish(
        self,
        snapshot: SessionSkillSnapshot,
        directory: AccountSkillDirectoryState,
        tree: SkillTreeManifest,
        digest: str,
        branches: list[PublicationBranch],
        members: dict[str, tuple[UUID, UUID]],
        candidates: list[AccountLocalSkill],
    ) -> SkillCheckpoint:
        """
        发布完整目录并只推进原会话确实改动的分支。

        :param snapshot (SessionSkillSnapshot): 原始精确快照
        :param directory (AccountSkillDirectoryState): CAS 前的账户目录
        :param tree (SkillTreeManifest): 已验证完整合并结果
        :param digest (str): 已持久化树摘要
        :param branches (list[PublicationBranch]): 精确分支和写入标志
        :param members (dict[str, tuple[UUID, UUID]]): 未更新前的完整成员引用
        :param candidates (list[AccountLocalSkill]): 本次可激活的本地来源
        :return SkillCheckpoint: 已推进到权威 head 的完整目录
        """
        runtime = self.context.runtime
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=snapshot.user_id,
            account_id=snapshot.account_id,
            scope="directory",
            directory_epoch=directory.epoch,
            content_digest=digest,
            tree_digest=digest,
            parent_id=directory.head_checkpoint_id,
            source_session_reference_id=snapshot.session_reference_id,
        )
        runtime.add(checkpoint)
        await runtime.flush()
        for branch in branches:
            if branch.changed:
                view = await self._item_checkpoint(
                    snapshot, tree, digest, branch.state, branch.item.entry_name, checkpoint
                )
                if not await self.context.repository.advance_branch(branch.state, view.id):
                    raise SkillContentError("HEAD_CHANGED", "branch changed during publication")
                members[branch.item.entry_name] = (branch.state.id, view.id)
        for candidate in candidates:
            assert candidate.default_revision_id is not None
            state = AccountSkillState(
                id=uuid4(),
                user_id=snapshot.user_id,
                account_id=snapshot.account_id,
                local_skill_id=candidate.id,
                local_revision_id=candidate.default_revision_id,
                installation_epoch=1,
            )
            runtime.add(state)
            await runtime.flush()
            view = await self._item_checkpoint(
                snapshot, tree, digest, state, candidate.name, checkpoint
            )
            state.head_checkpoint_id = view.id
            candidate.status = "active"
            members[candidate.name] = (state.id, view.id)
        roots = {entry.path for entry in tree.entries if "/" not in entry.path}
        for name, (state_id, checkpoint_id) in sorted(members.items()):
            if name not in roots:
                continue
            runtime.add(
                SkillDirectoryMember(
                    directory_checkpoint_id=checkpoint.id,
                    entry_name=name,
                    user_id=snapshot.user_id,
                    account_id=snapshot.account_id,
                    state_id=state_id,
                    checkpoint_id=checkpoint_id,
                )
            )
            checkpoint.invalid_skill_format = (
                checkpoint.invalid_skill_format or await self._invalid(snapshot.user_id, tree, name)
            )
        await runtime.flush()
        if not await self.context.repository.advance_directory(directory, checkpoint.id):
            raise SkillContentError("HEAD_CHANGED", "directory changed during publication")
        return checkpoint

    async def _item_checkpoint(
        self,
        snapshot: SessionSkillSnapshot,
        tree: SkillTreeManifest,
        digest: str,
        state: AccountSkillState,
        name: str,
        backing: SkillCheckpoint,
    ) -> SkillCheckpoint:
        """
        删除或无效格式也保留同身份视图，不退回原始包。

        :param snapshot (SessionSkillSnapshot): 原始会话归属
        :param tree (SkillTreeManifest): 完整合并树
        :param digest (str): 已保存树摘要
        :param state (AccountSkillState): 目标运行分支
        :param name (str): 固定条目名称
        :param backing (SkillCheckpoint): 同次创建的确切完整目录
        :return SkillCheckpoint: 新建的同分支完整树视图
        """
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=snapshot.user_id,
            account_id=snapshot.account_id,
            scope="item",
            state_id=state.id,
            state_epoch=state.epoch,
            backing_directory_id=backing.id,
            subtree_prefix=name,
            content_digest=digest,
            tree_digest=digest,
            parent_id=state.head_checkpoint_id,
            source_session_reference_id=snapshot.session_reference_id,
            invalid_skill_format=await self._invalid(snapshot.user_id, tree, name),
        )
        self.context.runtime.add(checkpoint)
        await self.context.runtime.flush()
        return checkpoint

    async def _invalid(self, user_id: UUID, tree: SkillTreeManifest, name: str) -> bool:
        """
        格式错误只成为诊断，不阻止安全运行字节的持久化。

        :param user_id (UUID): 内容归属用户
        :param tree (SkillTreeManifest): 完整结果树
        :param name (str): 已知身份名称
        :return bool: 是否无法作为有效技能再次启动
        """
        try:
            entry = _skill_document(tree, name + "/SKILL.md")
            _metadata(await self.store.read_prefix(user_id, entry, 65_544), name)
        except SkillContentError:
            return True
        return False
