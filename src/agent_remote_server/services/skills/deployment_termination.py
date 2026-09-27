"""
先永久撤销原尝试执行权，再以精确 Helper 排空凭据原子发布失败或替代终态。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import NodeTask, NodeTaskResult
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_deployment_terminations import SkillDeploymentTermination
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentTerminatedResult,
    SkillDeploymentTerminationIntent,
    SkillDeploymentTerminationObservation,
    SkillDeploymentTerminationRequest,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import (
    RETRYABLE_ERRORS,
    current_attempts,
    save_projection,
)
from agent_remote_server.services.skills.deployment_result_context import (
    deployment_identity,
    original_deployment,
)
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.takeover_context import content_hash


@dataclass(frozen=True)
class DeploymentTerminationHistory:
    """
    原操作及完整尝试链允许历史终态重放而不覆盖后继。
    """

    operation: SkillOperation
    attempts: tuple[SkillDeploymentAttempt, ...]
    current: dict[UUID, SkillDeploymentAttempt]


class NodeDeploymentTermination:
    """
    所有步骤复用原用户后任务锁序，独立于新准入开关与内容可用性。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        使用调用方提交的原请求事务。

        :param session (AsyncSession): 当前事务
        """
        self.session = session
        self.repository = SkillDeploymentTaskRepository(session)

    async def request(
        self,
        node_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        request: SkillDeploymentTerminationRequest,
    ) -> SkillDeploymentTerminationIntent:
        """
        当前原领取可以永久撤权，过期不等于排空且不阻止安全撤权。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 原任务数据库身份
        :param attempt_id (UUID): 原始尝试
        :param request (SkillDeploymentTerminationRequest): 不可变原撤权请求
        :return SkillDeploymentTerminationIntent: 唯一持久撤权指令
        """
        binding, task = await original_deployment(self.session, node_id, task_id, attempt_id)
        saved = await self.repository.termination(attempt_id)
        if saved is not None:
            intent = termination_intent(binding, saved)
            if intent.request != request or task.status == "succeeded":
                raise conflict("original termination request differs")
            return intent
        if (
            task.status not in {"leased", "running", "expired"}
            or task.retry_count != request.lease_attempt
            or await self.repository.results(task)
        ):
            raise conflict("deployment is terminal or poll changed")
        history = await self._attempts(binding)
        operation, current = history.operation, history.current
        if current[binding.account_id].id != attempt_id or current[
            binding.account_id
        ].status not in {"pending", "running"}:
            raise conflict("original deployment attempt is no longer active")
        replaced = operation.replacement_id is not None
        if request.error_code == "OPERATION_SUPERSEDED" and not replaced:
            raise conflict("original operation has not been superseded")
        async with retention_mutation(self.session, binding.user_id):
            saved = SkillDeploymentTermination(
                attempt_id=attempt_id,
                id=uuid4(),
                lease_attempt=request.lease_attempt,
                error_code=request.error_code,
                outcome="superseded" if replaced else "failed",
            )
            self.session.add(saved)
            await self.session.flush()
        return termination_intent(binding, saved)

    async def lookup(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID
    ) -> SkillDeploymentTerminationIntent | None:
        """
        原身份只读恢复撤权指令，既不续租也不推断本地排空。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 原任务身份
        :param attempt_id (UUID): 原尝试身份
        :return SkillDeploymentTerminationIntent | None: 原指令或尚未撤权
        """
        binding, task = await original_deployment(self.session, node_id, task_id, attempt_id)
        saved = await self.repository.termination(attempt_id)
        if saved is None:
            return None
        if task.status == "succeeded" or task.retry_count < saved.lease_attempt:
            raise conflict("termination conflicts with original task")
        return termination_intent(binding, saved)

    async def confirm(
        self,
        node_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        result: SkillDeploymentTerminatedResult,
    ) -> SkillDeploymentTerminationObservation:
        """
        精确排空后一次提交任务、目标、结果及保留时钟，重放不改变后继尝试。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 原任务身份
        :param attempt_id (UUID): 原尝试身份
        :param result (SkillDeploymentTerminatedResult): 原指令和本地排空结果
        :return SkillDeploymentTerminationObservation: 已持久确认的原终态
        """
        binding, task = await self._context(node_id, task_id, attempt_id, result)
        if await self._accepted(binding, task, result):
            return observation(task, result, True)
        if task.status not in {"pending", "leased", "running", "expired"}:
            raise conflict("original task is no longer awaiting drain")
        history = await self._attempts(binding)
        operation, current = history.operation, history.current
        attempt = current[binding.account_id]
        if attempt.id != attempt_id or attempt.status not in {"pending", "running"}:
            raise conflict("original attempt changed before drain")
        async with retention_mutation(self.session, binding.user_id):
            intent = result.intent
            attempt.status, attempt.error_code, attempt.retryable = (
                intent.outcome,
                intent.error_code,
                intent.retryable,
            )
            save_projection(operation, current)
            task.status, task.lease_until = terminal_status(intent), None
            self.session.add(
                NodeTaskResult(
                    node_task_id=task.id,
                    task_id=task.task_id,
                    status="failed",
                    result={
                        "result": result.model_dump(mode="json"),
                        "lease_attempt": task.retry_count,
                    },
                    error=None,
                    finished_at=datetime.now(UTC),
                )
            )
            await self.session.flush()
        return observation(task, result, True)

    async def inspect(
        self,
        node_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        result: SkillDeploymentTerminatedResult,
    ) -> SkillDeploymentTerminationObservation:
        """
        只读核对原终态结果，不重新授予内容或准备权限。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 原任务身份
        :param attempt_id (UUID): 原尝试身份
        :param result (SkillDeploymentTerminatedResult): 精确待确认提案
        :return SkillDeploymentTerminationObservation: 历史提交观察
        """
        binding, task = await self._context(node_id, task_id, attempt_id, result)
        return observation(task, result, await self._accepted(binding, task, result))

    async def _context(
        self,
        node_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        result: SkillDeploymentTerminatedResult,
    ) -> tuple[SkillDeploymentTask, NodeTask]:
        """
        撤权和本地凭据必须逐字段匹配原持久绑定。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 原任务身份
        :param attempt_id (UUID): 原尝试身份
        :param result (SkillDeploymentTerminatedResult): 原始排空结果
        :return tuple[SkillDeploymentTask, NodeTask]: 锁内原绑定与任务
        """
        binding, task = await original_deployment(self.session, node_id, task_id, attempt_id)
        saved = await self.repository.termination(attempt_id)
        if (
            saved is None
            or termination_intent(binding, saved) != result.intent
            or result.drain.binding != deployment_identity(binding)
            or result.drain.helper_receipt_id.int == 0
            or task.retry_count < saved.lease_attempt
        ):
            raise conflict("termination result differs from original intent")
        return binding, task

    async def _accepted(
        self, binding: SkillDeploymentTask, task: NodeTask, result: SkillDeploymentTerminatedResult
    ) -> bool:
        """
        缺失、重复或漂移的终态不能伪装成尚未提交。

        :param binding (SkillDeploymentTask): 原始绑定
        :param task (NodeTask): 原始任务
        :param result (SkillDeploymentTerminatedResult): 精确原结果
        :return bool: 是否已提交完全相同的终态
        """
        saved = await self.repository.results(task)
        if not saved:
            if task.status in {"succeeded", "failed", "cancelled"}:
                raise conflict("terminal deployment receipt is missing")
            return False
        expected = {"result": result.model_dump(mode="json"), "lease_attempt": task.retry_count}
        if (
            len(saved) != 1
            or saved[0].node_task_id != task.id
            or saved[0].task_id != task.task_id
            or saved[0].status != "failed"
            or task.status != terminal_status(result.intent)
            or task.lease_until is not None
            or saved[0].error is not None
            or saved[0].result is None
            or content_hash(saved[0].result) != content_hash(expected)
        ):
            raise conflict("terminal deployment receipt differs")
        history = await self._attempts(binding)
        original = next((row for row in history.attempts if row.id == binding.attempt_id), None)
        if original is None or (original.status, original.error_code, original.retryable) != (
            result.intent.outcome,
            result.intent.error_code,
            result.intent.retryable,
        ):
            raise conflict("original terminated attempt differs")
        return True

    async def _attempts(self, binding: SkillDeploymentTask) -> DeploymentTerminationHistory:
        """
        验证完整尝试链，历史重放保留原行而不覆盖后继投影。

        :param binding (SkillDeploymentTask): 原始部署绑定
        :return DeploymentTerminationHistory: 原操作、完整链和当前投影
        """
        operation = await SkillLibraryRepository(self.session).operation(
            binding.user_id, binding.operation_id
        )
        if operation is None:
            raise conflict("original operation is missing")
        attempts = await SkillDeploymentAttemptRepository(self.session).attempts(
            binding.user_id, binding.operation_id
        )
        return DeploymentTerminationHistory(
            operation, attempts, current_attempts(operation, attempts)
        )


def termination_intent(
    binding: SkillDeploymentTask, saved: SkillDeploymentTermination
) -> SkillDeploymentTerminationIntent:
    """
    从不可变数据库记录构造精确指令，不读取实时配置或内容。

    :param binding (SkillDeploymentTask): 原始绑定
    :param saved (SkillDeploymentTermination): 原撤权意图
    :return SkillDeploymentTerminationIntent: 原始排空指令
    """
    return SkillDeploymentTerminationIntent.model_validate(
        {
            "version": 1,
            "intent_id": saved.id,
            "binding": deployment_identity(binding),
            "request": {"lease_attempt": saved.lease_attempt, "error_code": saved.error_code},
            "outcome": saved.outcome,
            "error_code": "OPERATION_SUPERSEDED"
            if saved.outcome == "superseded"
            else saved.error_code,
            "retryable": saved.outcome == "failed" and saved.error_code in RETRYABLE_ERRORS,
        }
    )


def terminal_status(intent: SkillDeploymentTerminationIntent) -> str:
    """
    目标替代映射为任务取消，普通失败仍使用失败阶段。

    :param intent (SkillDeploymentTerminationIntent): 原撤权分类
    :return str: 对应任务终态
    """
    return "cancelled" if intent.outcome == "superseded" else "failed"


def observation(
    task: NodeTask, result: SkillDeploymentTerminatedResult, accepted: bool
) -> SkillDeploymentTerminationObservation:
    """
    只返回原提案和提交事实，不授予执行权。

    :param task (NodeTask): 锁内原任务
    :param result (SkillDeploymentTerminatedResult): 原终态结果
    :param accepted (bool): 是否精确匹配持久结果
    :return SkillDeploymentTerminationObservation: 有界只读观察
    """
    return SkillDeploymentTerminationObservation.model_validate(
        {
            "result": result,
            "accepted": accepted,
            "current_lease_attempt": task.retry_count,
            "task_status": task.status,
        }
    )


def conflict(message: str) -> SkillContentError:
    """
    保持终止协议的稳定有界冲突分类。

    :param message (str): 无私有内容的稳定原因
    :return SkillContentError: 专用终止冲突
    """
    return SkillContentError("DEPLOYMENT_TERMINATION_CONFLICT", message)
