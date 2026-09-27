"""
编排可重试的用户技能库事务及可解释查询。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models.skill_library import SkillInstallation, SkillOperation
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.repositories.skill_deployment_lifecycle import (
    SkillDeploymentLifecycleRepository,
)
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_preparation_lifecycle import (
    SkillPreparationLifecycleRepository,
)
from agent_remote_server.schemas.skill_deployment import SkillDeploymentPlan
from agent_remote_server.schemas.skill_library import (
    SkillAddRequest,
    SkillLibraryRequest,
    SkillProvenance,
    SkillRemoveRequest,
    SkillRollbackRequest,
    SkillRuleRequest,
    SkillScope,
    SkillSource,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_results import (
    SkillAccountRuleView,
    SkillErrorView,
    SkillInstallationView,
    SkillLibraryView,
    SkillLocalView,
    SkillMutationData,
    SkillOperationTarget,
    SkillResult,
    SkillRevisionView,
)
from agent_remote_server.schemas.skill_rules import SkillRuleOverride
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.deployment_attempts import (
    current_attempts,
    initial_attempts,
)
from agent_remote_server.services.skills.deployment_capability import supports_deployment
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_plans import capture_plans
from agent_remote_server.services.skills.deployment_replacement import validate_replacement
from agent_remote_server.services.skills.deployment_supersession import supersede_deployments
from agent_remote_server.services.skills.deployment_validation import saved_plans
from agent_remote_server.services.skills.library_context import LibraryChange, LibraryContext
from agent_remote_server.services.skills.library_install import add_skills
from agent_remote_server.services.skills.library_local import local_view
from agent_remote_server.services.skills.library_rules import change_rules
from agent_remote_server.services.skills.library_selection import Selection, account_selections
from agent_remote_server.services.skills.library_versions import (
    remove_skill,
    rollback_skill,
    update_skill,
)
from agent_remote_server.services.skills.preparation_lifecycle import PreparationLifecycle
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.rules import resolve_skill_rule
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


class SkillLibraryService:
    """
    单一事务边界执行安装、版本选择与覆盖，调用方负责最终提交。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, settings: Settings | None = None
    ) -> None:
        """
        绑定当前请求事务和受保护字节存储。

        :param session (AsyncSession): 异步数据库事务
        :param store (PrivateObjectStore): 私有内容存储
        :param settings (Settings | None): 显式部署策略，缺失时不授予待执行资格
        """
        self._session = session
        self._repository = SkillLibraryRepository(session)
        self._store = store
        self._settings = settings

    async def execute(
        self, user_id: UUID, request: SkillLibraryRequest
    ) -> SkillResult[SkillMutationData]:
        """
        幂等检查优先于代数检查，失败的批量操作整体回滚保存点。

        :param user_id (UUID): 来自认证身份的用户标识
        :param request (SkillLibraryRequest): 严格类型化的命令请求
        :return SkillResult[SkillMutationData]: 可在断线后重新查询的原始受理结果
        """
        raw = request.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with retention_mutation(self._session, user_id):
            library = await self._repository.lock_library(user_id)
            previous = await self._repository.operation_by_key(user_id, request.idempotency_key)
            if previous is not None:
                if previous.request_digest != digest:
                    raise SkillContentError(
                        "IDEMPOTENCY_CONFLICT", "key already belongs to another command"
                    )
                return await self._saved_result(previous)
            if library.generation != request.expected_generation:
                raise SkillContentError(
                    "GENERATION_CONFLICT",
                    "library changed; refresh the plan",
                    details={
                        "expected": request.expected_generation,
                        "current": library.generation,
                    },
                )
            context = LibraryContext(self._repository, self._store, user_id, library.generation)
            previous_selections = await account_selections(self._repository, user_id)
            change = await _dispatch(context, request)
            if change.changed:
                library.generation += 1
                await self._repository.flush()
                lifecycle = PreparationLifecycle(
                    self._repository,
                    SkillPreparationRepository(self._session),
                    SkillPreparationLifecycleRepository(self._session),
                )
                for item in change.skills:
                    await lifecycle.library_changed(item)
            targets = await self._targets(user_id, change, previous_selections)
            data = SkillMutationData(
                generation=library.generation,
                skill_ids=[item.id for item in change.skills]
                + [local.id for local in change.local_skills],
                revision_ids=[revision.id for revision in change.revisions],
                changed=change.changed,
                warnings=change.warnings,
                targets=targets,
            )
            operation = SkillOperation(
                id=uuid4(),
                user_id=user_id,
                idempotency_key=request.idempotency_key,
                request_digest=digest,
                request_json=raw,
                result_json=data.model_dump(mode="json"),
                generation=library.generation,
                status="preparing"
                if any(target.readiness == "pending" for target in targets)
                else "failed"
                if any(target.readiness == "unsupported" for target in targets)
                else "stored",
                committed=True,
                retryable=False,
                plan_version=1,
            )
            self._repository.add(operation)
            await self._repository.flush()
            await capture_plans(
                self._repository, SkillDeploymentRepository(self._session), operation, targets
            )
            await initial_attempts(self._session, operation, targets)
            operation.result_json = data.model_dump(mode="json")
            await self._repository.flush()
            if change.changed:
                await supersede_deployments(self._session, operation)
            return _operation_result(operation)

    async def status(self, user_id: UUID, operation_id: UUID) -> SkillResult[SkillMutationData]:
        """
        只返回当前用户的配置受理和目标状态。

        :param user_id (UUID): 认证用户标识
        :param operation_id (UUID): 原始操作标识
        :return SkillResult[SkillMutationData]: 原操作的持久化结果
        """
        await self._repository.read_library(user_id)
        operation = await self._repository.operation(user_id, operation_id)
        if operation is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "operation not found")
        return await self._saved_result(operation)

    async def status_by_key(self, user_id: UUID, key: str) -> SkillResult[SkillMutationData]:
        """
        接收响应丢失时仅凭本地持久化幂等键找回原操作，不重放变更。

        :param user_id (UUID): 认证用户标识
        :param key (str): 原始客户端幂等键
        :return SkillResult[SkillMutationData]: 原操作的持久化结果
        """
        await self._repository.read_library(user_id)
        operation = await self._repository.operation_by_key(user_id, key)
        if operation is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "operation not found")
        return await self._saved_result(operation)

    async def status_by_retry_key(
        self, user_id: UUID, operation_id: UUID, key: str
    ) -> SkillResult[SkillMutationData]:
        """
        仅以同用户同操作的真实重试回执恢复，不猜测当前状态代表已受理。

        :param user_id (UUID): 认证所有者
        :param operation_id (UUID): 原配置操作
        :param key (str): 独立重试请求键
        :return SkillResult[SkillMutationData]: 原操作当前观察
        """
        await self._repository.read_library(user_id)
        receipt = await SkillDeploymentAttemptRepository(self._session).retry_by_key(user_id, key)
        if receipt is None or receipt.operation_id != operation_id:
            raise SkillContentError("OPERATION_NOT_FOUND", "retry receipt not found")
        return await self.status(user_id, operation_id)

    async def _saved_result(self, operation: SkillOperation) -> SkillResult[SkillMutationData]:
        """
        校验原配置计划再返回回执，不从后来配置修补缺失或损坏的计划。

        :param operation (SkillOperation): 已授权的历史操作
        :return SkillResult[SkillMutationData]: 保持原始受理身份的结果
        """
        targets, entries = await SkillDeploymentRepository(self._session).rows(
            operation.user_id, operation.id
        )
        plans = await execution_plans(self._session, saved_plans(operation, targets, entries))
        current_attempts(
            operation,
            await SkillDeploymentAttemptRepository(self._session).attempts(
                operation.user_id, operation.id
            ),
        )
        if operation.replacement_id is not None and operation.attempts_version == 1:
            replacement = await self._repository.operation(
                operation.user_id, operation.replacement_id
            )
            newer: tuple[SkillDeploymentPlan, ...] = ()
            if replacement is not None:
                rows = await SkillDeploymentRepository(self._session).rows(
                    replacement.user_id, replacement.id
                )
                newer = await execution_plans(self._session, saved_plans(replacement, *rows))
            validate_replacement(operation, plans, replacement, newer)
        return _operation_result(operation)

    async def list_skills(
        self, user_id: UUID, *, scope: SkillScope | None = None
    ) -> SkillLibraryView:
        """
        读取同一配置代数下的当前用户库和指定范围字段来源。

        :param user_id (UUID): 认证用户标识
        :param scope (SkillScope | None): 可选解释范围
        :return SkillLibraryView: 当前库配置
        """
        library = await self._repository.read_library(user_id)
        generation = library.generation if library is not None else 0
        context = LibraryContext(self._repository, self._store, user_id, generation)
        if scope is not None:
            await context.validate_scope(scope)
        items = [
            await self._view(item, scope)
            for item in await self._repository.list_installations(user_id)
        ]
        local_items = []
        if scope is not None and scope.account_id is not None:
            local_items = [
                await local_view(self._repository.local, item)
                for item in await self._repository.local.visible(user_id, scope.account_id)
            ]
        return SkillLibraryView(generation=generation, items=items, local_items=local_items)

    async def info(
        self, user_id: UUID, identifier: str, *, scope: SkillScope | None = None
    ) -> SkillInstallationView | SkillLocalView:
        """
        在同一读锁下消歧库与账户本地来源。

        :param user_id (UUID): 认证用户
        :param identifier (str): 原始名称或稳定身份
        :param scope (SkillScope | None): 明确查询范围
        :return SkillInstallationView | SkillLocalView: 精确来源的详情
        """
        library = await self._repository.read_library(user_id)
        context = LibraryContext(
            self._repository, self._store, user_id, library.generation if library is not None else 0
        )
        if scope is not None:
            await context.validate_scope(scope)
        item = await context.resolve_source(identifier, scope)
        if isinstance(item, AccountLocalSkill):
            return await local_view(self._repository.local, item)
        return await self._view(item, scope)

    async def _view(
        self, item: SkillInstallation, scope: SkillScope | None
    ) -> SkillInstallationView:
        """
        将已授权模型转换为完整、类型化的版本和覆盖解释。

        :param item (SkillInstallation): 当前用户安装记录
        :param scope (SkillScope | None): 可选目标范围
        :return SkillInstallationView: 规则、版本和来源视图
        """
        assert item.default_revision_id is not None
        tools, accounts = await self._repository.rules(item)
        tool_views = {
            row.tool_type: SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
            for row in tools
        }
        account_views = {
            str(row.account_id): SkillAccountRuleView(
                enabled=row.enabled, revision_id=row.revision_id, tool_type=row.tool_type
            )
            for row in accounts
        }
        effective = None
        if scope is not None:
            account_rule = None
            tool = scope.tools[0] if scope.tools else None
            if scope.account_id is not None:
                account = await self._repository.account(item.user_id, scope.account_id)
                assert account is not None
                tool = account.tool_type
                account_rule = account_views.get(str(account.id))
            effective = resolve_skill_rule(
                item.default_enabled,
                item.default_revision_id,
                tool_views.get(tool) if tool is not None else None,
                account_rule,
                exclusion_reason="removed" if item.removed else None,
            )
        revisions = [
            SkillRevisionView(
                id=revision.id,
                number=revision.number,
                content_digest=revision.content_digest,
                retained=revision.retained,
                provenance=SkillProvenance.model_validate(revision.provenance_json),
                metadata=revision.metadata_json,
            )
            for revision in await self._repository.revisions(item.user_id, item.id)
        ]
        return SkillInstallationView(
            id=item.id,
            name=item.name,
            epoch=item.epoch,
            removed=item.removed,
            source=SkillSource.model_validate(item.source_json),
            tracking=item.tracking_json,
            default_enabled=item.default_enabled,
            default_revision_id=item.default_revision_id,
            revisions=revisions,
            tool_overrides=tool_views,
            account_overrides=account_views,
            effective=effective,
        )

    async def _targets(
        self, user_id: UUID, change: LibraryChange, previous: dict[UUID, Selection]
    ) -> list[SkillOperationTarget]:
        """
        固定目标并按显式策略受理兼容节点，离线等待不代表已经执行。

        :param user_id (UUID): 认证用户标识
        :param change (LibraryChange): 已验证的实际命令变更
        :param previous (dict[UUID, Selection]): 变更前账户的实际启用选择
        :return list[SkillOperationTarget]: 各账户存储或部署状态
        """
        if not change.deploy:
            return []
        current = await account_selections(self._repository, user_id)
        sources = {item.id for item in change.skills} | {item.id for item in change.local_skills}
        unfinished = await SkillDeploymentLifecycleRepository(self._session).unfinished_accounts(
            user_id,
            tuple(item.id for item in change.skills)
            + tuple(item.id for item in change.local_skills),
        )
        targets = []
        for account in await self._repository.accounts(user_id):
            if change.scope.account_id is not None and account.id != change.scope.account_id:
                continue
            if change.scope.tools and account.tool_type not in change.scope.tools:
                continue
            if (
                change.scope.account_id is None
                and account.id not in unfinished
                and previous.get(account.id, frozenset()) == current[account.id]
                and not (
                    not change.changed
                    and any(source_id in sources for _, source_id, _, _ in current[account.id])
                )
            ):
                continue
            bound = account.affinity_node_id is not None
            node = (
                await SkillDeploymentTaskRepository(self._session).node(account.affinity_node_id)
                if account.affinity_node_id is not None
                else None
            )
            supported = (
                account.status == "active"
                and node is not None
                and self._settings is not None
                and supports_deployment(
                    node, account.runtime_backend, account.tool_type, self._settings, fresh=False
                )
            )
            targets.append(
                SkillOperationTarget(
                    account_id=account.id,
                    node_id=account.affinity_node_id,
                    readiness="pending" if supported else "unsupported" if bound else "stored",
                    deploy_on_first_use=not bound,
                    error_code="SKILL_MANAGER_UNSUPPORTED" if bound and not supported else None,
                )
            )
        return targets


async def _dispatch(context: LibraryContext, request: SkillLibraryRequest) -> LibraryChange:
    """
    将互斥命令交给各自狭窄模块，避免巨型条件服务。

    :param context (LibraryContext): 用户库事务上下文
    :param request (SkillLibraryRequest): 命令请求
    :return LibraryChange: 具体模块产生的实际变更
    """
    match request:
        case SkillAddRequest():
            return await add_skills(context, request)
        case SkillUpdateRequest():
            return await update_skill(context, request)
        case SkillRuleRequest():
            return await change_rules(context, request)
        case SkillRollbackRequest():
            return await rollback_skill(context, request)
        case SkillRemoveRequest():
            return await remove_skill(context, request)
    raise AssertionError("unhandled skill command")


def _operation_result(operation: SkillOperation) -> SkillResult[SkillMutationData]:
    """
    将保存的原始受理结果转换为稳定协议封套。

    :param operation (SkillOperation): 同用户的持久化操作
    :return SkillResult[SkillMutationData]: 配置提交和各目标状态
    """
    data = SkillMutationData.model_validate(operation.result_json)
    data.replacement_id = operation.replacement_id
    errors = [
        SkillErrorView(
            code=target.error_code,
            message={
                "SKILL_MANAGER_UNSUPPORTED": "node does not support managed skills",
                "NODE_UNAVAILABLE": "original deployment node is unavailable",
                "TRANSFER_FAILED": "deployment transfer failed; original input is retained",
                "QUOTA_EXCEEDED": "deployment storage quota was exceeded",
                "DEPLOYMENT_INTERRUPTED": "original deployment attempt was interrupted",
                "STATE_MIGRATION_REQUIRED": "deployment requires state conflict resolution",
                "OPERATION_SUPERSEDED": "deployment was replaced by a newer operation",
            }.get(target.error_code, "deployment target failed"),
            object_id=str(target.account_id),
        )
        for target in data.targets
        if target.error_code is not None
    ]
    return SkillResult(
        operation_id=operation.id,
        status=operation.status,
        committed=operation.committed,
        retryable=operation.retryable and operation.attempts_version == 1,
        data=data,
        errors=errors,
    )
