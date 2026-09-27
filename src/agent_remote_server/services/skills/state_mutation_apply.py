"""
将状态重置恢复结果与选中分支纪元及完整目录一起原子发布。
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_state_operations import SkillStateOperationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_state_commands import SkillCurrentStateView, SkillStateTarget
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.services.skills.state_mutation_plan import StateMutationPlan
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class StateMutationApply:
    """
    新建分支仍在同一个保存点内，最后目录失败不会留下半次重置。
    """

    queries: SkillStateQueryService
    local: SkillLocalRepository
    repository: SkillStateOperationRepository
    store: PrivateObjectStore

    async def publish(
        self,
        user_id: UUID,
        current: SkillCurrentStateView,
        plan: StateMutationPlan,
        operation_id: UUID,
    ) -> UUID:
        """
        全部字节齐备后建立目录与成员视图，再交换每个 head 和纪元。

        :param user_id (UUID): 当前用户
        :param current (SkillCurrentStateView): 已比较的精确前置条件
        :param plan (StateMutationPlan): 完整验证的待发布树
        :param operation_id (UUID): 本次幂等操作身份
        :return UUID: 新发布完整目录身份
        """
        runtime = self.queries.runtime
        directory = await runtime.directory(user_id, current.selector.account_id)
        assert directory is not None
        if (
            directory.head_checkpoint_id != current.precondition.directory_head_id
            or directory.epoch != current.precondition.directory_epoch
            or directory.mode != current.precondition.directory_mode
        ):
            raise SkillContentError("HEAD_CHANGED", "directory changed after state preview")
        upload = await self.queries.content.begin(
            user_id, f"state-command:{operation_id}", plan.result, "account_directory"
        )
        tree = await self.queries.content.complete(user_id, upload.id)
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=user_id,
            account_id=directory.account_id,
            scope="directory",
            directory_epoch=directory.epoch + int(current.selector.scope == "account-directory"),
            content_digest=tree.digest,
            tree_digest=tree.digest,
            parent_id=plan.previous.id,
        )
        runtime.add(checkpoint)
        await runtime.flush()
        members = {
            member.entry_name: (member.state_id, member.checkpoint_id) for member in plan.preserved
        }
        for target in current.precondition.targets:
            branch = await self._branch(user_id, directory.account_id, target)
            view = SkillCheckpoint(
                id=uuid4(),
                user_id=user_id,
                account_id=directory.account_id,
                scope="item",
                state_id=branch.id,
                state_epoch=branch.epoch + 1,
                backing_directory_id=checkpoint.id,
                subtree_prefix=target.name,
                content_digest=tree.digest,
                tree_digest=tree.digest,
                parent_id=branch.head_checkpoint_id,
                invalid_skill_format=await self._invalid(user_id, plan.result, target.name),
            )
            runtime.add(view)
            await runtime.flush()
            if not await self.repository.advance_branch(branch, view.id):
                raise SkillContentError("HEAD_CHANGED", "branch changed during state command")
            members[target.name] = (branch.id, view.id)
        roots = {entry.path for entry in plan.result.entries if "/" not in entry.path}
        for name, (state_id, item_id) in sorted(members.items()):
            if name not in roots:
                continue
            runtime.add(
                SkillDirectoryMember(
                    directory_checkpoint_id=checkpoint.id,
                    entry_name=name,
                    user_id=user_id,
                    account_id=directory.account_id,
                    state_id=state_id,
                    checkpoint_id=item_id,
                )
            )
            checkpoint.invalid_skill_format |= await self._invalid(user_id, plan.result, name)
        await runtime.flush()
        if not await self.repository.advance_directory(
            directory, checkpoint.id, current.selector.scope == "account-directory"
        ):
            raise SkillContentError("HEAD_CHANGED", "directory changed during state command")
        return checkpoint.id

    async def _branch(
        self, user_id: UUID, account_id: UUID, target: SkillStateTarget
    ) -> AccountSkillState:
        """
        缺失目标可由显式重置恢复建立，不从另一版本继承运行数据。

        :param user_id (UUID): 当前用户
        :param account_id (UUID): 已授权账户
        :param target (SkillStateTarget): 原始精确选择
        :return AccountSkillState: 已存在或本事务新建的同源分支
        """
        if target.origin == "user_library":
            branch = await self.queries.runtime.branch(
                user_id, account_id, target.skill_id, target.installation_epoch, target.revision_id
            )
        else:
            branch = await self.local.branch(
                user_id, account_id, target.skill_id, target.revision_id
            )
        if branch is not None and (
            branch.id != target.state_id
            or branch.epoch != target.state_epoch
            or branch.head_checkpoint_id != target.head_checkpoint_id
            or branch.expired != target.expired
        ):
            raise SkillContentError("HEAD_CHANGED", "branch changed after state preview")
        if branch is None and target.state_id is not None:
            raise SkillContentError("HEAD_CHANGED", "selected branch disappeared")
        if branch is None:
            branch = AccountSkillState(
                id=uuid4(),
                user_id=user_id,
                account_id=account_id,
                installation_id=target.skill_id if target.origin == "user_library" else None,
                base_revision_id=target.revision_id if target.origin == "user_library" else None,
                local_skill_id=target.skill_id if target.origin == "account_local" else None,
                local_revision_id=target.revision_id if target.origin == "account_local" else None,
                installation_epoch=target.installation_epoch,
                epoch=1,
                expired=False,
            )
            self.queries.runtime.add(branch)
            await self.queries.runtime.flush()
        return branch

    async def _invalid(self, user_id: UUID, tree: SkillTreeManifest, name: str) -> bool:
        """
        无效说明或显式删除保留恢复内容，不偷偷恢复原始包。

        :param user_id (UUID): 内容所有者
        :param tree (SkillTreeManifest): 完整结果
        :param name (str): 已知成员名称
        :return bool: 是否不再满足工具技能格式
        """
        try:
            entry = _skill_document(tree, name + "/SKILL.md")
            _metadata(await self.store.read_prefix(user_id, entry, 65_544), name)
        except SkillContentError:
            return True
        return False
