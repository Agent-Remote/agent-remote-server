"""
只读原始 prune 回执与明细，当前删除进度独立于不可变受理。
"""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_prune_operations import SkillPruneOperationRepository
from agent_remote_server.schemas.skill_prune import (
    PruneDeletionProgress,
    PruneReceipt,
    PruneReceiptPage,
)
from agent_remote_server.schemas.skill_prune_rows import PruneDisclosure
from agent_remote_server.services.skills.content_errors import SkillContentError

_ROW = TypeAdapter[PruneDisclosure](PruneDisclosure)


class PruneQueries:
    """
    不依赖私有存储、签名器、保留索引或原始业务输入。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        使用同请求只读事务和所有者约束仓储。

        :param session (AsyncSession): 请求事务
        """
        self.repository = SkillPruneOperationRepository(session)

    async def by_key(self, user_id: UUID, key: str) -> PruneReceipt:
        """
        断线后按原键恢复同一不可变受理。

        :param user_id (UUID): 活跃认证所有者
        :param key (str): 原始请求键
        :return PruneReceipt: 已受理原结果
        """
        row = await self.repository.operation(user_id, key)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "prune operation not found")
        return PruneReceipt.model_validate(row.response_json)

    async def by_id(self, user_id: UUID, operation_id: UUID) -> PruneReceipt:
        """
        原身份不能跨所有者读取回执。

        :param user_id (UUID): 活跃认证所有者
        :param operation_id (UUID): 原操作身份
        :return PruneReceipt: 已受理原结果
        """
        row = await self.repository.operation_by_id(user_id, operation_id)
        if row is None:
            raise SkillContentError("OPERATION_NOT_FOUND", "prune operation not found")
        return PruneReceipt.model_validate(row.response_json)

    async def entries(
        self,
        user_id: UUID,
        operation_id: UUID,
        offset: int = 0,
        limit: int = 100,
    ) -> PruneReceiptPage:
        """
        分页恢复全部原披露，只允许有效的连续位置。

        :param user_id (UUID): 当前认证用户
        :param operation_id (UUID): 原操作身份
        :param offset (int): 第一条原序号
        :param limit (int): 单页上限
        :return PruneReceiptPage: 原始完整披露的连续页
        """
        receipt = await self.by_id(user_id, operation_id)
        if not 0 <= offset <= receipt.disclosure_rows or not 1 <= limit <= 100:
            raise SkillContentError("INVALID_REQUEST", "invalid prune receipt page")
        rows = tuple(
            _ROW.validate_python(row)
            for row in await self.repository.entries(
                user_id,
                operation_id,
                offset,
                limit,
            )
        )
        end = offset + len(rows)
        if len(rows) != min(limit, receipt.disclosure_rows - offset):
            raise ValueError("incomplete persisted prune disclosure")
        return PruneReceiptPage(
            operation_id=operation_id,
            offset=offset,
            total=receipt.disclosure_rows,
            rows=rows,
            next_offset=end if end < receipt.disclosure_rows else None,
        )

    async def progress(self, user_id: UUID, operation_id: UUID) -> PruneDeletionProgress:
        """
        认证原操作后独立读取任务进度，不修改回执中的原逻辑结算。

        :param user_id (UUID): 当前认证用户
        :param operation_id (UUID): 原操作身份
        :return PruneDeletionProgress: 原任务当前物理进度
        """
        await self.by_id(user_id, operation_id)
        pending, completed, waiting_bytes, deleted_bytes, retrying = await self.repository.progress(
            user_id, operation_id
        )
        return PruneDeletionProgress(
            operation_id=operation_id,
            pending_tasks=pending,
            completed_tasks=completed,
            pending_file_bytes=waiting_bytes,
            deleted_file_bytes=deleted_bytes,
            retrying_tasks=retrying,
        )
