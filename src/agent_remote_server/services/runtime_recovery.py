"""
以独立租约任务检查原迁移完成证明，保留失败历史及账户写入禁令。
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, NodeTask, ToolAccount, ToolAccountProfile, User
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.repositories.skill_imports import SkillImportRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.repositories.tool_accounts import ToolAccountRepository
from agent_remote_server.schemas.runtime_recovery import (
    RuntimeRecoveryAuthorization,
    RuntimeRecoveryBinding,
    RuntimeRecoveryData,
    RuntimeRecoveryLease,
    RuntimeRecoveryRequest,
)

RECOVERY_TASK = "recover_tool_account_runtime"
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
RECOVERY_FAILURE = {
    "code": "RUNTIME_MIGRATION_RECOVERY_REQUIRED",
    "message": "Original backend migration still requires recovery; no account work was restarted.",
}

RECOVERY_REPAIR_FAILURE = {
    "code": "RUNTIME_MIGRATION_RECOVERY_REQUIRED",
    "message": (
        "Original backend migration still requires recovery; source restoration is not confirmed."
    ),
}


class RuntimeRecoveryService:
    """
    在账户用户锁和精确任务锁内受理、授权并结算独立恢复检查。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用外层事务，调用方负责最终提交。

        :param session (AsyncSession): 外层请求事务
        """
        self.session = session

    async def submit(
        self, actor: User, account_id: UUID, request: RuntimeRecoveryRequest
    ) -> RuntimeRecoveryData:
        """
        同键只读重放，不以新键覆盖尚未结束的检查。

        :param actor (User): 已通过管理员校验的操作者
        :param account_id (UUID): 原账户
        :param request (RuntimeRecoveryRequest): 精确原任务及独立键
        :return RuntimeRecoveryData: 原有或新受理检查
        """
        task_id = f"{RECOVERY_TASK}:{account_id}:{request.request_id}"
        observed = await self._task(task_id)
        account = await self._account(account_id, read_only=observed is not None)
        saved = await self._task(task_id)
        if saved is not None:
            binding = self._binding(saved)
            if (
                binding.original_task_id != request.original_task_id
                or binding.action != request.action
                or binding.user_id != account.user_id
                or binding.tool_account_id != account.id
            ):
                raise _conflict()
            return RuntimeRecoveryData(binding=binding, status=saved.status)
        original = await self._task(request.original_task_id)
        if original is None or request.request_id.int == 0:
            raise _conflict()
        payload = original.payload
        try:
            binding = RuntimeRecoveryBinding.model_validate(
                {
                    **(
                        {
                            "version": 3 if request.action == "repair_source" else 2,
                            "action": request.action,
                        }
                        if request.action
                        else {}
                    ),
                    "task_id": task_id,
                    "task_record_id": uuid4(),
                    "original_task_id": original.task_id,
                    "original_task_record_id": original.id,
                    "node_id": original.node_id,
                    "user_id": payload.get("user_id"),
                    "tool_account_id": payload.get("tool_account_id"),
                    "tool_type": payload.get("tool_type"),
                    "source_runtime_backend": payload.get("source_runtime_backend"),
                    "target_runtime_backend": payload.get("target_runtime_backend"),
                }
            )
        except ValidationError as error:
            raise _conflict() from error
        if binding.tool_account_id != account.id or binding.user_id != account.user_id:
            raise _conflict()
        profile, migration = await self._current(binding, account)
        previous = migration.get("recovery_task_id")
        if previous is not None:
            prior = await self._task(str(previous))
            if prior is None or prior.task_type != RECOVERY_TASK or prior.status not in TERMINAL:
                raise _conflict()
        task = NodeTask(
            id=binding.task_record_id,
            node_id=binding.node_id,
            task_id=task_id,
            task_type=RECOVERY_TASK,
            status="pending",
            payload=binding.model_dump(mode="json"),
            retry_count=0,
        )
        self.session.add(task)
        profile.profile_json = {
            **profile.profile_json,
            "runtime_migration": {
                **migration,
                "recovery_task_id": task_id,
                "recovery_status": "pending",
            },
        }
        self.session.add(
            AuditLog(
                actor_user_id=actor.id,
                action="tool_accounts.runtime_migration_recovery",
                target_type="tool_account",
                target_id=str(account.id),
                details={"task_id": task_id, "original_task_id": original.task_id},
            )
        )
        await self.session.flush()
        return RuntimeRecoveryData(binding=binding, status=task.status)

    async def status(self, account_id: UUID, key: UUID) -> RuntimeRecoveryData:
        """
        只读查询原键，不创建任务或更换原迁移。

        :param account_id (UUID): 管理员选择的账户
        :param key (UUID): 原请求键
        :return RuntimeRecoveryData: 原检查当前状态
        """
        account = await self._account(account_id, read_only=True)
        task = await self._task(f"{RECOVERY_TASK}:{account_id}:{key}")
        if task is None:
            raise _not_found()
        binding = self._binding(task)
        if binding.user_id != account.user_id or binding.tool_account_id != account.id:
            raise _not_found()
        return RuntimeRecoveryData(binding=binding, status=task.status)

    async def authorize(self, node_id: UUID, task_id: str) -> RuntimeRecoveryAuthorization:
        """
        节点读取原始身份后重新持锁，活动租约与当前档案同时匹配才允许检查。

        :param node_id (UUID): 认证节点
        :param task_id (str): 恢复逻辑任务
        :return RuntimeRecoveryAuthorization: 原始绑定及即时租约轮次
        """
        task = await self._task(task_id)
        if task is None or task.node_id != node_id or task.task_type != RECOVERY_TASK:
            raise _not_found()
        binding = self._binding(task)
        account = await self._account(binding.tool_account_id, read_only=True)
        task = await self._task(task_id, lock=True)
        if task is None or task.node_id != node_id or self._binding(task) != binding:
            raise _not_found()
        self._lease(task)
        _, migration = await self._current(binding, account)
        if (
            migration.get("recovery_task_id") != task_id
            or migration.get("recovery_status") != "pending"
        ):
            raise _conflict()
        return RuntimeRecoveryAuthorization(binding=binding, lease_attempt=task.retry_count)

    async def renew(
        self,
        node_id: UUID,
        task_id: str,
        expected: RuntimeRecoveryAuthorization,
        lease_seconds: int,
    ) -> RuntimeRecoveryLease:
        """
        在原用户和任务锁内仅延长未过期的原始恢复轮次。

        :param node_id (UUID): 认证节点身份
        :param task_id (str): 精确恢复逻辑任务
        :param expected (RuntimeRecoveryAuthorization): 调用方持有的完整原始授权
        :param lease_seconds (int): 配置的租约秒数
        :return RuntimeRecoveryLease: 原轮次及保守计时所需的服务器时间
        """
        current = await self.authorize(node_id, task_id)
        if current != expected or lease_seconds <= 0:
            raise _conflict()
        task = await self._task(task_id, lock=True)
        if task is None or self._binding(task) != current.binding:
            raise _not_found()
        self._lease(task)
        now = datetime.now(UTC)
        duration = min(lease_seconds, 300)
        task.lease_until = now + timedelta(seconds=duration)
        await self.session.flush()
        return RuntimeRecoveryLease(
            authorization=current,
            server_time=now,
            lease_until=task.lease_until,
            renew_after_milliseconds=max(1, duration * 1000 // 3),
        )

    async def result(
        self, task: NodeTask, value: dict[str, object], outcome: Literal["succeeded", "failed"]
    ) -> bool:
        """
        首次结果再次校验当前租约，历史精确重放不得修改较新迁移。

        :param task (NodeTask): 当前认证节点的任务
        :param value (dict[str, object]): 固定无内容结果
        :param outcome (Literal["succeeded", "failed"]): 恢复检查终态
        :return bool: 是否已经受理完全相同的结果
        """
        if task.task_type != RECOVERY_TASK:
            return False
        binding = self._binding(task)
        observed = await NodeRepository(self.session).get_task_result(task.task_id)
        account = await self._account(binding.tool_account_id, read_only=observed is not None)
        current = await self._task(task.task_id, lock=True)
        if (
            current is None
            or self._binding(current) != binding
            or account.user_id != binding.user_id
        ):
            raise _conflict()
        saved = await NodeRepository(self.session).get_task_result(task.task_id)
        if saved is not None:
            expected = saved.result if outcome == "succeeded" else saved.error
            if (
                saved.node_task_id != task.id
                or saved.status != outcome
                or task.status != outcome
                or not _same(value, expected)
            ):
                raise _conflict()
            return True
        authorization = await self.authorize(binding.node_id, binding.task_id)
        expected_value: dict[str, object] = (
            {"recovered": True, "authorization": authorization.model_dump(mode="json")}
            if outcome == "succeeded"
            else {
                **(
                    RECOVERY_REPAIR_FAILURE
                    if binding.action == "repair_source"
                    else RECOVERY_FAILURE
                ),
                "lease_attempt": authorization.lease_attempt,
            }
        )
        if not _same(value, expected_value):
            raise _conflict()
        profile, migration = await self._current(binding, account)
        changes: dict[str, object] = {"recovery_status": outcome}
        if outcome == "succeeded":
            account.runtime_backend = (
                binding.source_runtime_backend
                if binding.action in {"verify_source", "repair_source"}
                else binding.target_runtime_backend
            )
            if account.status == "migrating":
                previous = migration.get("previous_status")
                account.status = previous if isinstance(previous, str) else "active"
            changes["status"] = (
                "rolled_back"
                if binding.action in {"verify_source", "repair_source"}
                else "succeeded"
            )
        profile.profile_json = {
            **profile.profile_json,
            "runtime_migration": {**migration, **changes},
        }
        return False

    async def _account(self, account_id: UUID, read_only: bool = False) -> ToolAccount:
        """
        获取账户所属用户锁并刷新活动归属，不接受已变更所有者。

        :param account_id (UUID): 账户身份
        :param read_only (bool): 是否只锁定已有计量行而不修改任何行
        :return ToolAccount: 持锁后活动用户的原账户
        """
        account = await self.session.get(ToolAccount, account_id)
        if account is None:
            raise _not_found()
        user_id = account.user_id
        storage = SkillStorageRepository(self.session)
        if read_only:
            if await storage.lock_existing_usage(user_id) is None:
                raise _not_found()
        else:
            await storage.lock_usage(user_id)
        account = await SkillImportRepository(self.session).account(user_id, account_id)
        if account is None:
            raise _not_found()
        return account

    async def _task(self, task_id: str, lock: bool = False) -> NodeTask | None:
        """
        刷新逻辑任务，结果与授权按用户锁后任务锁的顺序串行。

        :param task_id (str): 逻辑任务身份
        :param lock (bool): 是否锁定租约行
        :return NodeTask | None: 原任务或空值
        """
        statement = (
            select(NodeTask)
            .where(NodeTask.task_id == task_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return await self.session.scalar(statement)

    def _binding(self, task: NodeTask) -> RuntimeRecoveryBinding:
        """
        验证保存的完整绑定及当前任务身份。

        :param task (NodeTask): 保存的恢复任务
        :return RuntimeRecoveryBinding: 已验证不可变绑定
        """
        try:
            binding = RuntimeRecoveryBinding.model_validate(task.payload)
        except ValidationError as error:
            raise _conflict() from error
        if (
            task.task_type != RECOVERY_TASK
            or binding.task_id != task.task_id
            or binding.task_record_id != task.id
            or binding.node_id != task.node_id
            or not _same(task.payload, binding.model_dump(mode="json"))
        ):
            raise _conflict()
        return binding

    def _lease(self, task: NodeTask) -> None:
        """
        终态和过期任务不能读取授权或写入新的检查结果。

        :param task (NodeTask): 已锁定任务
        """
        lease = task.lease_until
        if (
            task.status not in {"leased", "running"}
            or task.retry_count <= 0
            or lease is None
            or lease.replace(tzinfo=lease.tzinfo or UTC) <= datetime.now(UTC)
        ):
            raise _not_found()

    async def _current(
        self, binding: RuntimeRecoveryBinding, account: ToolAccount
    ) -> tuple[ToolAccountProfile, dict[str, object]]:
        """
        只允许原终态迁移仍独占旧模式账户，数据库终态不替代节点写入者证明。

        :param binding (RuntimeRecoveryBinding): 原始绑定
        :param account (ToolAccount): 已刷新并持用户锁的账户
        :return tuple[ToolAccountProfile, dict[str, object]]: 原档案及迁移记录
        """
        original = await self._task(binding.original_task_id)
        profile = await self.session.scalar(
            select(ToolAccountProfile)
            .where(ToolAccountProfile.tool_account_id == account.id)
            .execution_options(populate_existing=True)
        )
        migration = profile.profile_json.get("runtime_migration") if profile is not None else None
        if (
            original is None
            or original.id != binding.original_task_record_id
            or original.task_type != "migrate_tool_account_runtime"
            or original.node_id != binding.node_id
            or original.status not in {"failed", "cancelled", "expired"}
        ):
            raise _conflict()
        expected = {
            "user_id": str(binding.user_id),
            "tool_account_id": str(binding.tool_account_id),
            "tool_type": binding.tool_type,
            "source_runtime_backend": binding.source_runtime_backend,
            "target_runtime_backend": binding.target_runtime_backend,
        }
        if any(original.payload.get(key) != value for key, value in expected.items()):
            raise _conflict()
        if (
            account.id != binding.tool_account_id
            or account.user_id != binding.user_id
            or account.affinity_node_id != binding.node_id
            or account.tool_type != binding.tool_type
            or account.runtime_backend != binding.source_runtime_backend
            or binding.source_runtime_backend == binding.target_runtime_backend
        ):
            raise _conflict()
        if (
            profile is None
            or not isinstance(migration, dict)
            or migration.get("task_id") != original.task_id
            or migration.get("status") not in {"pending", "failed", "recovery_required"}
            or any(
                migration.get(key) != expected[key]
                for key in ("source_runtime_backend", "target_runtime_backend")
            )
        ):
            raise _conflict()
        directory = await SkillRuntimeRepository(self.session).directory(
            account.user_id, account.id
        )
        if directory is not None and directory.mode != "legacy":
            raise _conflict()
        if await ToolAccountRepository(self.session).list_active_sessions(account.id):
            raise _conflict()
        return profile, migration


def _same(value: object, expected: object) -> bool:
    """
    精确比较 JSON 类型，避免真假值与整数相等掩盖协议变化。

    :param value (object): 收到的结果
    :param expected (object): 固定原结果
    :return bool: 是否完全相同
    """
    return json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)


def _conflict() -> ApiError:
    """
    返回不携带私有内容的原始恢复冲突。

    :return ApiError: 原始授权不再满足
    """
    return ApiError(
        code="RUNTIME_MIGRATION_RECOVERY_CONFLICT",
        message="Original migration is not eligible for this recovery check.",
        status_code=409,
    )


def _not_found() -> ApiError:
    """
    隐藏外来任务或失效归属。

    :return ApiError: 原任务未授权
    """
    return ApiError(
        code="COMMON_NOT_FOUND", message="Migration recovery task was not found.", status_code=404
    )
