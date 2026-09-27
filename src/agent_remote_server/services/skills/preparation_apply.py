"""
完整发布迁移目标分支与账户目录，保留非目标来源的原有引用。
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_state_commands import SkillCurrentStateView
from agent_remote_server.services.skills.branch_publication import (
    BranchPublicationPlan,
    SkillBranchPublisher,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.preparation_plan import BranchPreparationPlan
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class BranchPreparationApply:
    """
    只在外层保存点与用户锁内执行完整结果交换。
    """

    queries: SkillStateQueryService
    repository: SkillPublicationRepository
    store: PrivateObjectStore

    async def branch(self, user_id: UUID, selection: SkillCurrentStateView) -> AccountSkillState:
        """
        再次匹配原始目标前置条件，冲突也只创建无 head 的明确目标身份。

        :param user_id (UUID): 所有者
        :param selection (SkillCurrentStateView): 原始选择
        :return AccountSkillState: 本次精确目标分支
        """
        target = selection.precondition.targets[0]
        branch = await self.queries.runtime.branch(
            user_id,
            selection.selector.account_id,
            target.skill_id,
            target.installation_epoch,
            target.revision_id,
        )
        if branch is None:
            if target.state_id is not None:
                raise SkillContentError("HEAD_CHANGED", "target branch disappeared")
            branch = AccountSkillState(
                id=uuid4(),
                user_id=user_id,
                account_id=selection.selector.account_id,
                installation_id=target.skill_id,
                installation_epoch=target.installation_epoch,
                base_revision_id=target.revision_id,
                epoch=1,
                expired=False,
            )
            self.queries.runtime.add(branch)
            await self.queries.runtime.flush()
        elif (branch.id, branch.epoch, branch.head_checkpoint_id, branch.expired) != (
            target.state_id,
            target.state_epoch,
            target.head_checkpoint_id,
            target.expired,
        ):
            raise SkillContentError("HEAD_CHANGED", "target branch changed after preview")
        return branch

    async def publish(
        self,
        branch: AccountSkillState,
        selection: SkillCurrentStateView,
        plan: BranchPreparationPlan,
        operation_id: UUID,
    ) -> tuple[UUID, UUID]:
        """
        不改变版本分支纪元，任何最后目录 CAS 失败都让外层回滚全部引用。

        :param branch (AccountSkillState): 精确目标分支
        :param selection (SkillCurrentStateView): 已校验原始选择
        :param plan (BranchPreparationPlan): 完整计划
        :param operation_id (UUID): 独立受理身份
        :return tuple[UUID, UUID]: 成功目标检查点和完整目录检查点
        """
        if plan.mode == "resume":
            assert branch.head_checkpoint_id is not None
            return branch.head_checkpoint_id, plan.directory.id
        assert plan.result is not None
        assert selection.precondition.directory_epoch is not None
        return await SkillBranchPublisher(self.queries, self.repository, self.store).publish(
            branch,
            BranchPublicationPlan(
                selection.precondition.targets[0].name,
                plan.directory,
                selection.precondition.directory_epoch,
                plan.members,
                plan.result,
            ),
            f"prepare:{operation_id}",
        )
