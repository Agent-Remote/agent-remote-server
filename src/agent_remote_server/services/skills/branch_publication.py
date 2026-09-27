"""
原子发布明确版本分支及其共同改动成员，完整目录与所有新视图一起提交。
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.services.skills.publication_context import subtree_entries
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass(frozen=True)
class BranchPublicationPlan:
    """
    只包含已验证结果和精确旧目录，发布器不重新选择规则或版本。
    """

    name: str
    directory: SkillCheckpoint
    directory_epoch: int
    members: list[SkillDirectoryMember]
    result: SkillTreeManifest


@dataclass
class SkillBranchPublisher:
    """
    调用方持有用户锁及保存点，失败不得单独提交部分 head。
    """

    queries: SkillStateQueryService
    repository: SkillPublicationRepository
    store: PrivateObjectStore

    async def publish(
        self, branch: AccountSkillState, plan: BranchPublicationPlan, operation_key: str
    ) -> tuple[UUID, UUID]:
        """
        全部检查点和目录成员一起推进，不改变分支或目录纪元。

        :param branch (AccountSkillState): 已核对旧 head 的目标分支
        :param plan (BranchPublicationPlan): 完整结果及精确目录前置条件
        :param operation_key (str): 内部唯一发布键
        :return tuple[UUID, UUID]: 已发布目标检查点及完整目录检查点
        """
        items, directory_id = await self.publish_many(branch, plan, operation_key, {})
        return items[plan.name], directory_id

    async def publish_many(
        self,
        branch: AccountSkillState,
        plan: BranchPublicationPlan,
        operation_key: str,
        related: dict[str, AccountSkillState],
    ) -> tuple[dict[str, UUID], UUID]:
        """
        所有实际修改成员与完整目录共用结果树，任何晚到 CAS 失败由外层整体回滚。

        :param branch (AccountSkillState): 精确迁移目标分支
        :param plan (BranchPublicationPlan): 完整已验证内容和旧目录
        :param operation_key (str): 内部持久发布键
        :param related (dict[str, AccountSkillState]): 已授权且确实改动的关联分支
        :return tuple[dict[str, UUID], UUID]: 各分支新视图与完整目录身份
        """
        runtime = self.queries.runtime
        directory = await runtime.directory(branch.user_id, branch.account_id)
        assert directory is not None
        if (
            directory.head_checkpoint_id != plan.directory.id
            or directory.epoch != plan.directory_epoch
            or directory.mode != "managed_v1"
        ):
            raise SkillContentError("HEAD_CHANGED", "directory changed after migration preview")
        branches = {plan.name: branch, **related}
        if plan.name in related or len({item.id for item in branches.values()}) != len(branches):
            raise SkillContentError("STATE_SCOPE_MISMATCH", "duplicate publication branch")
        before = {member.entry_name: member for member in plan.members}
        assert plan.directory.tree_digest is not None
        old_tree = await self.queries.content.read_tree(
            branch.user_id, "state", plan.directory.tree_digest
        )
        for name in before:
            changed = subtree_entries(old_tree, name) != subtree_entries(plan.result, name)
            if name != plan.name and changed and name not in related:
                raise SkillContentError("STATE_SCOPE_MISMATCH", "unplanned related source change")
            if name in related and not changed:
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "unchanged related source is not a write"
                )
        for name, state in branches.items():
            if (state.user_id, state.account_id) != (branch.user_id, branch.account_id):
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "publication branch has another owner"
                )
            if name != plan.name:
                member = before.get(name)
                if member is None or (member.state_id, member.checkpoint_id) != (
                    state.id,
                    state.head_checkpoint_id,
                ):
                    raise SkillContentError(
                        "HEAD_CHANGED", "related branch differs from saved directory"
                    )
        upload = await self.queries.content.begin(
            branch.user_id, f"{operation_key}:result", plan.result, "account_directory"
        )
        tree = await self.queries.content.complete(branch.user_id, upload.id)
        assert tree.digest == manifest_digest(plan.result)
        result = SkillCheckpoint(
            id=uuid4(),
            user_id=branch.user_id,
            account_id=branch.account_id,
            scope="directory",
            directory_epoch=plan.directory_epoch,
            content_digest=tree.digest,
            tree_digest=tree.digest,
            parent_id=plan.directory.id,
        )
        runtime.add(result)
        await runtime.flush()
        members = {name: (member.state_id, member.checkpoint_id) for name, member in before.items()}
        roots = {entry.path for entry in plan.result.entries if "/" not in entry.path}
        items = {}
        for name, state in sorted(branches.items()):
            invalid = await self._invalid(state.user_id, plan.result, name)
            item = SkillCheckpoint(
                id=uuid4(),
                user_id=state.user_id,
                account_id=state.account_id,
                scope="item",
                state_id=state.id,
                state_epoch=state.epoch,
                backing_directory_id=result.id,
                subtree_prefix=name,
                content_digest=tree.digest,
                tree_digest=tree.digest,
                parent_id=state.head_checkpoint_id,
                invalid_skill_format=invalid,
            )
            runtime.add(item)
            await runtime.flush()
            if not await self.repository.advance_branch(state, item.id):
                raise SkillContentError(
                    "HEAD_CHANGED", "migration branch changed during publication"
                )
            items[name] = item.id
            members.pop(name, None)
            if name in roots:
                members[name] = (state.id, item.id)
            result.invalid_skill_format |= invalid
        for name, (state_id, checkpoint_id) in sorted(members.items()):
            previous = await runtime.checkpoint(branch.user_id, branch.account_id, checkpoint_id)
            assert previous is not None
            result.invalid_skill_format |= previous.invalid_skill_format
            runtime.add(
                SkillDirectoryMember(
                    directory_checkpoint_id=result.id,
                    entry_name=name,
                    user_id=branch.user_id,
                    account_id=branch.account_id,
                    state_id=state_id,
                    checkpoint_id=checkpoint_id,
                )
            )
        await runtime.flush()
        if not await self.repository.advance_directory(directory, result.id):
            raise SkillContentError(
                "HEAD_CHANGED", "migration directory changed during publication"
            )
        return items, result.id

    async def _invalid(self, user_id: UUID, tree: SkillTreeManifest, name: str) -> bool:
        """
        无效说明和真实删除保留完整结果，格式诊断不冒充数据损坏。

        :param user_id (UUID): 内容所有者
        :param tree (SkillTreeManifest): 完整发布结果
        :param name (str): 明确稳定成员名称
        :return bool: 是否不能作为有效技能说明再次启动
        """
        try:
            entry = _skill_document(tree, name + "/SKILL.md")
            _metadata(await self.store.read_prefix(user_id, entry, 65_544), name)
        except SkillContentError:
            return True
        return False
