"""
保护后端迁移的原始任务结果和账户写入准入，失败不等于源状态已恢复。
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask, ToolAccount, ToolAccountProfile
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository


async def require_runtime_migration_settled(
    session: AsyncSession, user_id: UUID, account_id: UUID
) -> None:
    """
    同一用户锁内读取档案，展示状态变更不能重新打开未完成的迁移。

    :param session (AsyncSession): 持锁至写入提交的外层事务
    :param user_id (UUID): 已授权账户所有者
    :param account_id (UUID): 已授权账户身份
    """
    await SkillStorageRepository(session).lock_usage(user_id)
    account = await session.get(ToolAccount, account_id, populate_existing=True)
    if account is None or account.user_id != user_id:
        raise ApiError(
            code="COMMON_NOT_FOUND", message="Tool account was not found.", status_code=404
        )
    profile = await session.scalar(
        select(ToolAccountProfile)
        .join(ToolAccount, ToolAccount.id == ToolAccountProfile.tool_account_id)
        .where(ToolAccount.id == account_id, ToolAccount.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    migration = profile.profile_json.get("runtime_migration") if profile is not None else None
    if migration is not None and (
        not isinstance(migration, dict)
        or migration.get("status") not in {"succeeded", "rolled_back"}
    ):
        raise ApiError(
            code="RUNTIME_MIGRATION_PENDING",
            message="Retained backend migration requires recovery before new account writes.",
            status_code=409,
        )


class RuntimeMigrationResultGuard:
    """
    在通用节点结果写入前核实原迁移身份，并阻止终态重放触发再次状态变更。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用节点结果事务和用户锁。

        :param session (AsyncSession): 外层结果事务
        """
        self.session = session

    async def authorize(
        self, task: NodeTask, value: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        精确历史结果可只读重放，新的结果必须属于当前原始迁移和活动任务。

        :param task (NodeTask): 当前认证节点拥有的任务
        :param value (dict[str, object]): 有界成功或失败内容
        :param outcome (Literal["succeeded", "failed"]): 结果类型
        :return bool: 是否已经完整接受同一终态结果
        """
        if task.task_type != "migrate_tool_account_runtime":
            return False
        account_id = _task_identity(task, "tool_account_id")
        user_id = _task_identity(task, "user_id")
        account = await self.session.get(ToolAccount, account_id)
        if account is None or account.user_id != user_id:
            raise _invalid_result()
        node_id = task.node_id
        await SkillStorageRepository(self.session).lock_usage(user_id)
        await self.session.refresh(task)
        if (
            task.node_id != node_id
            or _task_identity(task, "tool_account_id") != account_id
            or _task_identity(task, "user_id") != user_id
        ):
            raise _invalid_result()
        saved = await NodeRepository(self.session).get_task_result(task.task_id)
        if saved is not None:
            if (
                saved.node_task_id != task.id
                or saved.status != outcome
                or task.status != outcome
                or saved.result != (value if outcome == "succeeded" else None)
                or saved.error != (value if outcome == "failed" else None)
            ):
                raise _invalid_result()
            return True
        account = await self.session.get(ToolAccount, account_id, populate_existing=True)
        profile = await self.session.scalar(
            select(ToolAccountProfile)
            .where(ToolAccountProfile.tool_account_id == account_id)
            .execution_options(populate_existing=True)
        )
        migration = profile.profile_json.get("runtime_migration") if profile is not None else None
        source, target = (
            task.payload.get("source_runtime_backend"),
            task.payload.get("target_runtime_backend"),
        )
        if (
            account is None
            or account.user_id != user_id
            or account.affinity_node_id != task.node_id
            or task.task_type != "migrate_tool_account_runtime"
            or task.status not in {"leased", "running"}
            or account.runtime_backend != source
            or task.payload.get("tool_type") != account.tool_type
        ):
            raise _invalid_result()
        if (
            not isinstance(source, str)
            or source not in {"native", "docker_sandbox"}
            or not isinstance(target, str)
            or target not in {"native", "docker_sandbox"}
            or source == target
        ):
            raise _invalid_result()
        expected = {
            "task_id": task.task_id,
            "status": "pending",
            "source_runtime_backend": source,
            "target_runtime_backend": target,
        }
        if not isinstance(migration, dict) or any(
            migration.get(key) != value for key, value in expected.items()
        ):
            raise _invalid_result()
        if outcome == "succeeded" and (
            value.get("migrated") is not True
            or value.get("runtime_backend") != target
            or value.get("tool_account_id", str(account_id)) != str(account_id)
        ):
            raise _invalid_result()
        return False


def _task_identity(task: NodeTask, name: str) -> UUID:
    """
    只接受任务中规范的账户或用户 UUID。

    :param task (NodeTask): 当前节点任务
    :param name (str): 已知身份字段名
    :return UUID: 验证后的规范身份
    """
    value = task.payload.get(name)
    if not isinstance(value, str):
        raise _invalid_result()
    try:
        identity = UUID(value)
    except ValueError as error:
        raise _invalid_result() from error
    if str(identity) != value:
        raise _invalid_result()
    return identity


def _invalid_result() -> ApiError:
    """
    返回不含任务正文或宿主数据的稳定冲突错误。

    :return ApiError: 原始迁移结果不一致
    """
    return ApiError(
        code="RUNTIME_MIGRATION_RESULT_CONFLICT",
        message="Backend migration result differs from the original operation or saved result.",
        status_code=409,
    )
