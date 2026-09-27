"""
原始部署准备结果与目标就绪共享一次持久提交，历史观察不授予执行权。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask, NodeTaskResult
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_deployment_result import (
    SkillDeploymentPreparedResult,
    SkillDeploymentResultObservation,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import (
    current_attempts,
    save_projection,
)
from agent_remote_server.services.skills.deployment_content import NodeDeploymentContent
from agent_remote_server.services.skills.deployment_context import (
    deployment_authority,
)
from agent_remote_server.services.skills.deployment_digest import deployment_input_digest
from agent_remote_server.services.skills.deployment_result_context import (
    deployment_identity,
    original_deployment,
)
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.takeover_context import content_hash


class NodeDeploymentResults:
    """
    使用独立结果协议，不调用通用任务完成以绕过原始内容和状态校验。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        保留调用方提交的事务和完整部署授权配置。

        :param session (AsyncSession): 当前结果事务
        :param settings (Settings): 内容和能力配置
        """
        self.session, self.settings = session, settings
        self.repository = SkillDeploymentTaskRepository(session)
        self.content = NodeDeploymentContent(session, settings)

    async def confirm(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, result: SkillDeploymentPreparedResult
    ) -> SkillDeploymentResultObservation:
        """
        首次成功原子发布任务和尝试，完全相同的历史结果不重复改变投影。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 精确任务数据库身份
        :param attempt_id (UUID): 原始部署尝试
        :param result (SkillDeploymentPreparedResult): 精确准备结果
        :return SkillDeploymentResultObservation: 已持久接受的原结果
        """
        binding, task = await self._context(node_id, task_id, attempt_id, result)
        if await self._accepted(binding, task, result):
            return self._observation(task, result, True)
        async with retention_mutation(self.session, binding.user_id):
            content = await self.content.describe(
                node_id, task_id, attempt_id, result.lease_attempt
            )
            receipt = result.preparation
            if (
                receipt.helper_receipt_id.int == 0
                or receipt.directory_epoch != content.directory_epoch
                or receipt.generation != content.plan.generation
                or receipt.input_digest != deployment_input_digest(content)
            ):
                raise SkillContentError("DEPLOYMENT_RESULT_INVALID", "prepared input differs")
            # 完整输入验证可能耗时，提交前重新核验领取期限和实时配置。
            await self.content.authorize(node_id, task_id, attempt_id, result.lease_attempt)
            authority = await deployment_authority(
                self.session,
                self.settings,
                binding.user_id,
                binding.operation_id,
                binding.account_id,
                attempt_id,
            )
            deadline = task.lease_until
            if deadline is None or deadline.replace(tzinfo=deadline.tzinfo or UTC) <= datetime.now(
                UTC
            ):
                raise SkillContentError("DEPLOYMENT_LEASE_CHANGED", "deployment lease expired")
            authority.current[binding.account_id].status = "ready"
            save_projection(authority.operation, authority.current)
            task.status, task.lease_until = "succeeded", None
            self.session.add(
                NodeTaskResult(
                    node_task_id=task.id,
                    task_id=task.task_id,
                    status="succeeded",
                    result=result.model_dump(mode="json"),
                    error=None,
                    finished_at=datetime.now(UTC),
                )
            )
            await self.session.flush()
        return self._observation(task, result, True)

    async def inspect(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, result: SkillDeploymentPreparedResult
    ) -> SkillDeploymentResultObservation:
        """
        只读观察原结果，即使配置开关或内容保留期已改变也不重建新输入。

        :param node_id (UUID): 已认证原节点
        :param task_id (UUID): 精确任务数据库身份
        :param attempt_id (UUID): 原始尝试
        :param result (SkillDeploymentPreparedResult): 原待确认结果
        :return SkillDeploymentResultObservation: 不授予租约的历史提交事实
        """
        binding, task = await self._context(node_id, task_id, attempt_id, result)
        return self._observation(task, result, await self._accepted(binding, task, result))

    async def _context(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, result: SkillDeploymentPreparedResult
    ) -> tuple[SkillDeploymentTask, NodeTask]:
        """
        持久绑定先发现归属，按用户再任务锁序重读完整原始身份。

        :param node_id (UUID): 已认证节点
        :param task_id (UUID): 精确任务身份
        :param attempt_id (UUID): 精确原尝试
        :param result (SkillDeploymentPreparedResult): 原准备结果
        :return tuple[SkillDeploymentTask, NodeTask]: 锁内原绑定与任务
        """
        binding, task = await original_deployment(self.session, node_id, task_id, attempt_id)
        if (
            result.preparation.helper_receipt_id.int == 0
            or result.lease_attempt > task.retry_count
            or result.preparation.binding != deployment_identity(binding)
        ):
            raise SkillContentError(
                "DEPLOYMENT_RESULT_INVALID", "deployment result binding differs"
            )
        return binding, task

    async def _accepted(
        self, binding: SkillDeploymentTask, task: NodeTask, result: SkillDeploymentPreparedResult
    ) -> bool:
        """
        结果和已确认阶段必须一致，损坏或重复终态不能作为重新提交依据。

        :param binding (SkillDeploymentTask): 锁内原始绑定
        :param task (NodeTask): 锁内原始任务
        :param result (SkillDeploymentPreparedResult): 精确待比较结果
        :return bool: 是否已持久接受完全相同的原结果
        """
        saved = await self.repository.results(task)
        if not saved:
            if task.status in {"succeeded", "failed"}:
                raise SkillContentError("DEPLOYMENT_RESULT_CONFLICT", "terminal receipt is missing")
            return False
        if (
            len(saved) != 1
            or saved[0].node_task_id != task.id
            or saved[0].task_id != task.task_id
            or saved[0].status != "succeeded"
            or task.status != "succeeded"
            or saved[0].error is not None
            or task.retry_count != result.lease_attempt
            or task.lease_until is not None
            or saved[0].result is None
            or content_hash(saved[0].result) != content_hash(result.model_dump(mode="json"))
        ):
            raise SkillContentError(
                "DEPLOYMENT_RESULT_CONFLICT", "deployment result already differs"
            )
        operation = await SkillLibraryRepository(self.session).operation(
            binding.user_id, binding.operation_id
        )
        if operation is None:
            raise SkillContentError("DEPLOYMENT_RESULT_CONFLICT", "original operation is missing")
        current = current_attempts(
            operation,
            await SkillDeploymentAttemptRepository(self.session).attempts(
                binding.user_id, binding.operation_id
            ),
        )
        attempt = current.get(binding.account_id)
        if attempt is None or attempt.id != binding.attempt_id or attempt.status != "ready":
            raise SkillContentError("DEPLOYMENT_RESULT_CONFLICT", "deployment readiness differs")
        return True

    def _observation(
        self, task: NodeTask, result: SkillDeploymentPreparedResult, accepted: bool
    ) -> SkillDeploymentResultObservation:
        """
        返回有界元数据，不从历史就绪推断当前选择或文件可用。

        :param task (NodeTask): 锁内任务
        :param result (SkillDeploymentPreparedResult): 精确原结果
        :param accepted (bool): 是否完全匹配已提交结果
        :return SkillDeploymentResultObservation: 原始提交观察
        """
        return SkillDeploymentResultObservation.model_validate(
            {
                "result": result,
                "accepted": accepted,
                "current_lease_attempt": task.retry_count,
                "task_status": task.status,
            }
        )
