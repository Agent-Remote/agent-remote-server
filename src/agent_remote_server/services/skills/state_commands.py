"""
协调明确状态预览、重置恢复、幂等重放和全部目标的原子提交。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_state_operations import SkillStateOperation
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_state_operations import SkillStateOperationRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_state_commands import (
    SkillCurrentStateView,
    SkillStateBranchChanges,
    SkillStateCommand,
    SkillStateCommandView,
    SkillStateTarget,
)
from agent_remote_server.schemas.skill_state_queries import SkillStatePathDiff
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.state_mutation_apply import StateMutationApply
from agent_remote_server.services.skills.state_mutation_plan import StateMutationPlanner
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.services.skills.state_selection import StateSelection
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillStateCommandService:
    """
    外层请求只在完整回执返回后提交，任何异常回滚命令保存点。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        预览和真实命令共用完全相同的来源与内容验证。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 部署存储策略
        """
        self._session = session
        self.queries = SkillStateQueryService(session, store, policy)
        local = SkillLocalRepository(session)
        self.selection = StateSelection(self.queries, local)
        self.repository = SkillStateOperationRepository(session)
        self._planner = StateMutationPlanner(self.queries, local, store, policy)
        self._apply = StateMutationApply(self.queries, local, self.repository, store)

    async def execute(self, user_id: UUID, request: SkillStateCommand) -> SkillStateCommandView:
        """
        重放先于新规则检查，预览不保留任何引用，最后 CAS 失败全部回滚。

        :param user_id (UUID): 当前认证用户
        :param request (SkillStateCommand): 明确操作与期望状态
        :return SkillStateCommandView: 完整变更预览或不可变已提交结果
        """
        digest = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        async with retention_mutation(self._session, user_id, read_only=request.dry_run):
            if request.dry_run:
                await self.queries.library.read_library(user_id)
            else:
                await self.queries.library.lock_library(user_id)
                previous = await self.repository.operation(user_id, request.idempotency_key)
                if previous is not None:
                    if previous.request_digest != digest:
                        raise SkillContentError(
                            "IDEMPOTENCY_CONFLICT", "key already belongs to another state command"
                        )
                    return SkillStateCommandView.model_validate(previous.response_json)
            current = await self.selection.current(user_id, request.selector)
            _precondition(request, current)
            plan = await self._planner.prepare(user_id, request, current)
            before = {entry.path: entry for entry in plan.current.entries}
            after = {entry.path: entry for entry in plan.result.entries}
            changes = [
                SkillStatePathDiff(path=path, base=before.get(path), current=after.get(path))
                for path in sorted(before.keys() | after.keys(), key=str.encode)
                if before.get(path) != after.get(path)
            ]
            branch_changes = [
                await self._branch_changes(user_id, target, plan.result)
                for target in current.precondition.targets
            ]
            operation_id = None if request.dry_run else uuid4()
            checkpoint_id = None
            superseded = 0
            if operation_id is not None:
                checkpoint_id = await self._apply.publish(user_id, current, plan, operation_id)
                superseded = await SkillPreparationRepository(self._session).supersede(
                    user_id, request.selector.account_id, "state_" + request.action
                )
                superseded += await self.repository.supersede_conflicts(
                    user_id, request.selector.account_id, request.action
                )
            view = SkillStateCommandView(
                operation_id=operation_id,
                status="preview" if request.dry_run else "published",
                action=request.action,
                before=current,
                result_tree_digest=manifest_digest(plan.result),
                result_checkpoint_id=checkpoint_id,
                changes=changes,
                branch_changes=branch_changes,
                affected=current.precondition.targets,
                directory_epoch_advances=request.selector.scope == "account-directory",
                superseded_conflicts=superseded,
            )
            if operation_id is not None:
                assert checkpoint_id is not None
                self.queries.runtime.add(
                    SkillStateOperation(
                        id=operation_id,
                        user_id=user_id,
                        account_id=request.selector.account_id,
                        idempotency_key=request.idempotency_key,
                        request_digest=digest,
                        action=request.action,
                        scope="item" if request.selector.scope == "item" else "directory",
                        source_checkpoint_id=request.checkpoint_id,
                        result_checkpoint_id=checkpoint_id,
                        response_json=view.model_dump(mode="json"),
                    )
                )
                await self.queries.runtime.flush()
            return view

    async def _branch_changes(
        self, user_id: UUID, target: SkillStateTarget, result: SkillTreeManifest
    ) -> SkillStateBranchChanges:
        """
        当前目录可能展示另一版本，因此每个目标还需与自己的旧 head 比较。

        :param user_id (UUID): 当前用户
        :param target (SkillStateTarget): 精确受影响分支
        :param result (SkillTreeManifest): 完整拟发布结果
        :return SkillStateBranchChanges: 明确旧基线可用性的分支差异
        """
        baseline = SkillTreeManifest()
        available = True
        if target.head_checkpoint_id is not None:
            checkpoint = await self.queries.require(user_id, target.head_checkpoint_id)
            available = checkpoint.retained and checkpoint.tree_digest is not None
            if available:
                assert checkpoint.tree_digest is not None
                baseline = await self.queries.content.read_tree(
                    user_id, "state", checkpoint.tree_digest
                )
        before = {
            entry.path: entry
            for entry in baseline.entries
            if entry.path == target.name or entry.path.startswith(target.name + "/")
        }
        after = {
            entry.path: entry
            for entry in result.entries
            if entry.path == target.name or entry.path.startswith(target.name + "/")
        }
        changes = (
            [
                SkillStatePathDiff(path=path, base=before.get(path), current=after.get(path))
                for path in sorted(before.keys() | after.keys(), key=str.encode)
                if before.get(path) != after.get(path)
            ]
            if available
            else None
        )
        return SkillStateBranchChanges(
            skill_id=target.skill_id,
            state_id=target.state_id,
            checkpoint_id=target.head_checkpoint_id,
            baseline_available=available,
            changes=changes,
        )

    async def operation(self, user_id: UUID, key: str) -> SkillStateCommandView:
        """
        断线查询返回原始受理结果，不执行新命令或改写旧前置条件。

        :param user_id (UUID): 当前用户
        :param key (str): 原始幂等键
        :return SkillStateCommandView: 已提交原始结果
        """
        row = await self.repository.operation(user_id, key)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "state operation not found")
        return SkillStateCommandView.model_validate(row.response_json)

    async def operation_by_id(self, user_id: UUID, operation_id: UUID) -> SkillStateCommandView:
        """
        按原操作身份返回不可变已提交结果，不读取或重做当前计划。

        :param user_id (UUID): 当前认证用户
        :param operation_id (UUID): 原始状态操作身份
        :return SkillStateCommandView: 原始已提交回执
        """
        row = await self.repository.operation_by_id(user_id, operation_id)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "state operation not found")
        return SkillStateCommandView.model_validate(row.response_json)


def _precondition(request: SkillStateCommand, current: SkillCurrentStateView) -> None:
    """
    任何规则或 head 漂移都要求重新预览，纪元不能超过数据库上限。

    :param request (SkillStateCommand): 用户确认的旧状态
    :param current (SkillCurrentStateView): 已锁定当前状态
    """
    if request.expected != current.precondition:
        raise SkillContentError(
            "STATE_PRECONDITION_CHANGED",
            "selected rules or state changed; preview again",
            details={"current": current.precondition.model_dump(mode="json")},
        )
    if any((item.state_epoch or 1) >= 2**63 - 1 for item in current.precondition.targets):
        raise SkillContentError("LIMIT_EXCEEDED", "state epoch exhausted")
    if (
        request.selector.scope == "account-directory"
        and (current.precondition.directory_epoch or 1) >= 2**63 - 1
    ):
        raise SkillContentError("LIMIT_EXCEEDED", "directory epoch exhausted")
