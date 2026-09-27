"""
内部部署预约先完成账户准备，再原子绑定完整输入与精确 Node 任务。
"""

from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_deployment_termination import SkillDeploymentTerminatedResult
from agent_remote_server.services.skills.account_materialization import AccountMaterialization
from agent_remote_server.services.skills.account_preparation import AccountSkillPreparation
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import save_projection
from agent_remote_server.services.skills.deployment_context import (
    TASK_TYPE,
    deployment_authority,
    deployment_payload,
)
from agent_remote_server.services.skills.deployment_input import save_input, validate_input
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.takeover_admission import (
    SkillTakeoverAdmission,
    SkillTakeoverPending,
)
from agent_remote_server.services.skills.takeover_context import content_hash
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


class SkillDeploymentDispatch:
    """
    仅内部调用，调用方提交正常接管等待或迁移冲突；本层绝不声明执行就绪。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        使用部署固定的内容卷和配额。

        :param session (AsyncSession): 调用方最终提交的事务
        :param settings (Settings): 部署配置
        """
        self.session, self.settings = session, settings
        self.repository = SkillDeploymentTaskRepository(session)
        self.runtime = SkillRuntimeRepository(session)
        store = PrivateObjectStore(settings.skill_storage_root)
        self.materialization = AccountMaterialization(session, store, settings.skill_storage_policy)
        self.preparation = AccountSkillPreparation(session, store, settings.skill_storage_policy)

    async def reserve(
        self, user_id: UUID, operation_id: UUID, account_id: UUID, attempt_id: UUID
    ) -> SkillDeploymentTask | SkillTakeoverPending | tuple[UUID, ...]:
        """
        重试复用原完整输入；真实新预约、正常冲突和接管等待分别返回。

        :param user_id (UUID): 已授权所有者
        :param operation_id (UUID): 原配置操作
        :param account_id (UUID): 原目标账户
        :param attempt_id (UUID): 精确当前尝试
        :return SkillDeploymentTask | SkillTakeoverPending | tuple[UUID, ...]: 任务或准备等待
        """
        library = SkillLibraryRepository(self.session)
        if await library.operation(user_id, operation_id) is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "operation not found")
        async with retention_mutation(self.session, user_id):
            authority = await deployment_authority(
                self.session, self.settings, user_id, operation_id, account_id, attempt_id
            )
            bindings = await self.repository.bindings(user_id, operation_id, account_id)
            previous = next((row for row in bindings if row.attempt_id == attempt_id), None)
            if previous is not None:
                await validate_input(self.session, authority, previous)
                await self._task(previous, active=True)
                return previous
            if authority.current[account_id].status != "pending":
                raise SkillContentError("DEPLOYMENT_BINDING_MISSING", "running attempt has no task")
            if bindings:
                if len({row.checkpoint_id for row in bindings}) != 1:
                    raise SkillContentError("DEPLOYMENT_INPUT_CHANGED", "retry inputs disagree")
                checkpoint = await validate_input(self.session, authority, bindings[0])
                for binding in bindings:
                    await self._task(binding, active=False)
            else:
                takeover = await SkillTakeoverAdmission(self.session, self.settings).ensure(
                    authority.account, authority.node
                )
                if takeover is not None:
                    return takeover
                conflicts = await self.preparation.prepare(user_id, account_id)
                if conflicts:
                    attempt = authority.current[account_id]
                    attempt.status, attempt.error_code = (
                        "needs_resolution",
                        "STATE_MIGRATION_REQUIRED",
                    )
                    save_projection(authority.operation, authority.current)
                    await self.session.flush()
                    return conflicts
                checkpoint = await save_input(self.session, authority, self.materialization)
            binding = SkillDeploymentTask(
                attempt_id=attempt_id,
                user_id=user_id,
                operation_id=operation_id,
                account_id=account_id,
                node_id=authority.node.id,
                task_id=uuid4(),
                checkpoint_id=checkpoint.id,
                content_digest=checkpoint.content_digest,
                plan_digest=authority.plan.digest(),
            )
            await validate_input(self.session, authority, binding)
            await deployment_authority(
                self.session, self.settings, user_id, operation_id, account_id, attempt_id
            )
            self.session.add(
                NodeTask(
                    id=binding.task_id,
                    node_id=binding.node_id,
                    task_id=f"{TASK_TYPE}:{attempt_id}",
                    task_type=TASK_TYPE,
                    status="pending",
                    payload=deployment_payload(binding),
                )
            )
            await self.session.flush()
            self.session.add(binding)
            await self.session.flush()
            return binding

    async def _task(self, binding: SkillDeploymentTask, *, active: bool) -> None:
        """
        旧任务必须精确匹配且已结束，避免重试与旧执行同时消费输入。

        :param binding (SkillDeploymentTask): 原始任务绑定
        :param active (bool): 是否读取本次仍活动的预约
        """
        task = await self.runtime.task(binding.node_id, binding.task_id)
        allowed = {"pending", "leased", "running"} if active else {"failed", "cancelled"}
        if (
            task is None
            or task.task_type != TASK_TYPE
            or task.task_id != f"{TASK_TYPE}:{binding.attempt_id}"
            or content_hash(task.payload) != content_hash(deployment_payload(binding))
            or task.status not in allowed
        ):
            raise SkillContentError("DEPLOYMENT_TASK_CHANGED", "original deployment task changed")

        if not active:
            results = await self.repository.results(task)
            try:
                if len(results) != 1 or results[0].result is None:
                    raise ValueError("missing original termination receipt")
                result = SkillDeploymentTerminatedResult.model_validate(
                    results[0].result.get("result")
                )
                observed = await NodeDeploymentTermination(self.session).inspect(
                    binding.node_id, binding.task_id, binding.attempt_id, result
                )
                if not observed.accepted:
                    raise ValueError("original termination is not accepted")
            except (ValueError, ValidationError, SkillContentError) as error:
                raise SkillContentError(
                    "DEPLOYMENT_TASK_CHANGED", "original deployment is not durably drained"
                ) from error
