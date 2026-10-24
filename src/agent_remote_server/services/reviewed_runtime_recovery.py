"""
复用既有恢复事务，提供无内容观察和版本化原请求回执。
"""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask, ToolAccount, User
from agent_remote_server.schemas.reviewed_runtime_recovery import (
    RecoveryInspectionData,
    ReviewedRecoveryData,
    ReviewedRecoveryRequest,
    RuntimeRecoveryOriginal,
)
from agent_remote_server.schemas.runtime_recovery import RuntimeRecoveryData, RuntimeRecoveryRequest
from agent_remote_server.services.runtime_recovery import RuntimeRecoveryService


class ReviewedRuntimeRecoveryService:
    """
    新入口只增加审核身份比较，不新增执行能力或持久化协议。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用请求事务及已有恢复服务。

        :param session (AsyncSession): 当前请求事务
        """
        self.session = session
        self.recovery = RuntimeRecoveryService(session)

    async def inspect(self, account_id: UUID, task_id: str) -> RecoveryInspectionData:
        """
        仅观察精确原任务，不创建计量行或推测恢复准入。

        :param account_id (UUID): 当前管理员选择的账户
        :param task_id (str): 显式原逻辑任务
        :return RecoveryInspectionData: 安全且有界的原任务观察
        """
        account = await self.session.scalar(
            select(ToolAccount)
            .join(User, User.id == ToolAccount.user_id)
            .where(ToolAccount.id == account_id, User.status == "active")
            .execution_options(populate_existing=True)
        )
        original = await self.session.scalar(
            select(NodeTask)
            .where(
                NodeTask.task_id == task_id,
                NodeTask.task_type == "migrate_tool_account_runtime",
            )
            .execution_options(populate_existing=True)
        )
        if account is None or original is None:
            raise not_found()
        payload = original.payload
        if payload.get("tool_account_id") != str(account.id) or payload.get("user_id") != str(
            account.user_id
        ):
            raise not_found()
        try:
            projection = RuntimeRecoveryOriginal.model_validate(
                {
                    "task_id": original.task_id,
                    "task_record_id": str(original.id),
                    "user_id": payload.get("user_id"),
                    "tool_account_id": payload.get("tool_account_id"),
                    "node_id": str(original.node_id),
                    "tool_type": payload.get("tool_type"),
                    "source_runtime_backend": payload.get("source_runtime_backend"),
                    "target_runtime_backend": payload.get("target_runtime_backend"),
                }
            )
            return RecoveryInspectionData.model_validate(
                {
                    "schema_version": 1,
                    "original": projection,
                    "original_status": original.status,
                    "account_status": account.status,
                }
            )
        except ValidationError:
            raise invalid_state() from None

    async def submit(
        self, actor: User, account_id: UUID, request: ReviewedRecoveryRequest
    ) -> ReviewedRecoveryData:
        """
        将显式动作适配为已有版本，原事务核对审核投影。

        :param actor (User): 当前用户管理员
        :param account_id (UUID): 请求路径账户
        :param request (ReviewedRecoveryRequest): 完整确认请求
        :return ReviewedRecoveryData: 原始恢复绑定与自身状态
        """
        if request.expected_original.tool_account_id != str(account_id):
            raise ApiError(
                code="COMMON_VALIDATION_ERROR", message="Invalid recovery request.", status_code=422
            )
        legacy = RuntimeRecoveryRequest(
            original_task_id=request.expected_original.task_id, request_id=UUID(request.request_id)
        )
        if request.action != "verify_target":
            legacy = RuntimeRecoveryRequest(
                original_task_id=request.expected_original.task_id,
                request_id=UUID(request.request_id),
                action=request.action,
            )
        data = await self.recovery.submit(
            actor, account_id, legacy, expected_original=request.expected_original
        )
        return receipt(request.request_id, data)

    async def status(self, account_id: UUID, request_id: str) -> ReviewedRecoveryData:
        """
        只查询原键，不以当前档案重新判定历史受理身份。

        :param account_id (UUID): 原始账户
        :param request_id (str): 原始恢复键
        :return ReviewedRecoveryData: 原始任务当前状态
        """
        return receipt(request_id, await self.recovery.status(account_id, UUID(request_id)))


def receipt(request_id: str, data: RuntimeRecoveryData) -> ReviewedRecoveryData:
    """
    将已有版本绑定转换为严格状态回执，禁止回显未知状态。

    :param request_id (str): 规范原始恢复键
    :param data (RuntimeRecoveryData): 已有恢复结果
    :return ReviewedRecoveryData: 强版本回执
    """
    try:
        RuntimeRecoveryOriginal.from_binding(data.binding)
        return ReviewedRecoveryData.model_validate(
            {
                "schema_version": 1,
                "request_id": request_id,
                "action": data.binding.action or "verify_target",
                "binding": data.binding,
                "status": data.status,
            }
        )
    except ValidationError:
        raise invalid_state() from None


def invalid_state() -> ApiError:
    """
    对损坏或未知的保存状态返回固定安全错误。

    :return ApiError: 无内容的状态冲突
    """
    return ApiError(
        code="RUNTIME_MIGRATION_RECOVERY_CONFLICT",
        message="Original migration recovery state is unavailable.",
        status_code=409,
    )


def not_found() -> ApiError:
    """
    隐藏缺失或不属于当前账户的原始任务。

    :return ApiError: 未找到授权范围内的任务
    """
    return ApiError(
        code="COMMON_NOT_FOUND", message="Original migration was not found.", status_code=404
    )
