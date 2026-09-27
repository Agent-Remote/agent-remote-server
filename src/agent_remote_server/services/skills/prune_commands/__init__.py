"""
把完整预览、紧凑确认和不可变回执接入同一 prune 事务，已受理重放优先于旧输入访问。
"""

import hashlib
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_prune_operations import SkillPruneOperation
from agent_remote_server.repositories.skill_prune_operations import SkillPruneOperationRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_prune import (
    PruneCommand,
    PrunePreviewPage,
    PrunePreviewRequest,
    PruneReceipt,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.prune import SkillPruneService
from agent_remote_server.services.skills.prune_commands.digest import digest
from agent_remote_server.services.skills.prune_commands.disclosure import disclosures, summary
from agent_remote_server.services.skills.prune_commands.preview import preview_page
from agent_remote_server.services.skills.prune_commands.tokens import PruneTokens
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillPruneCommandService:
    """
    原受理命中只依赖所有者与精确原请求；新动作才检查凭据与完整内容计划。
    """

    def __init__(
        self,
        session: AsyncSession,
        store: PrivateObjectStore,
        policy: SkillStoragePolicy,
        secret: str,
    ) -> None:
        """
        构造不会访问文件的同事务依赖，签名密钥仅驻留内存。

        :param session (AsyncSession): 调用方持有的事务
        :param store (PrivateObjectStore): 延迟访问的私有内容卷
        :param policy (SkillStoragePolicy): 当前保留和额度策略
        :param secret (str): 当前部署签名密钥
        """
        self._session = session
        self._prune = SkillPruneService(session, store, policy)
        self._tokens = PruneTokens(secret)
        self._storage = SkillStorageRepository(session)
        self._repository = SkillPruneOperationRepository(session)

    async def preview(self, user_id: UUID, request: PrunePreviewRequest) -> PrunePreviewPage:
        """
        同用户锁内读取一致预览，不写任何业务行或保留时钟。

        :param user_id (UUID): 活跃认证用户
        :param request (PrunePreviewRequest): 明确范围和原签名游标
        :return PrunePreviewPage: 同一原计划的连续披露页
        """
        await self._storage.lock_existing_usage(user_id)
        return await preview_page(self._prune, self._tokens, user_id, request)

    async def execute(self, user_id: UUID, request: PruneCommand) -> PruneReceipt:
        """
        原键重放不加载保留图，完整执行和最后明细失败均由最外层保存点回滚。

        :param user_id (UUID): 活跃认证用户
        :param request (PruneCommand): 精确原键和最终确认凭据
        :return PruneReceipt: 待外层最终提交或原已受理的不可变回执
        """
        request_digest = digest(request)
        await self._storage.lock_existing_usage(user_id)
        previous = await self._repository.operation(user_id, request.idempotency_key)
        if previous is not None:
            if previous.request_digest != request_digest:
                raise SkillContentError(
                    "IDEMPOTENCY_CONFLICT", "key belongs to another prune request"
                )
            return PruneReceipt.model_validate(previous.response_json)
        envelope = self._tokens.verify(request.confirmation, user_id, "confirm")
        async with retention_mutation(self._session, user_id):
            plan = await self._prune.preview(
                user_id,
                envelope.binding.selector,
                all_unreferenced=envelope.binding.all_unreferenced,
                cutoff=envelope.binding.cutoff,
            )
            view = summary(plan)
            total = sum(1 for _ in disclosures(plan))
            if view.binding != envelope.binding or total != envelope.total:
                raise SkillContentError("HEAD_CHANGED", "confirmed prune plan has changed")
            result = await self._prune.apply(user_id, plan)
            receipt = PruneReceipt(
                operation_id=uuid4(),
                idempotency_key=request.idempotency_key,
                confirmation_fingerprint=hashlib.sha256(request.confirmation.encode()).hexdigest(),
                summary=view,
                disclosure_rows=total,
            )
            await self._repository.save(
                SkillPruneOperation(
                    id=receipt.operation_id,
                    user_id=user_id,
                    account_id=plan.scope.account_id,
                    idempotency_key=request.idempotency_key,
                    request_digest=request_digest,
                    plan_digest=view.binding.plan_digest,
                    response_json=receipt.model_dump(mode="json"),
                ),
                (row.model_dump(mode="json") for row in disclosures(plan, result)),
                result.content.deletion_ids,
            )
            return receipt
