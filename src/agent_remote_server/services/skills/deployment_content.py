"""
按固定部署任务提供原完整目录及有界文件授权，不触发准备或执行完成。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_deployment_content import (
    SkillDeploymentContent,
    SkillDeploymentIdentity,
    SkillDeploymentLease,
    SkillDeploymentMember,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.deployment_authorization import authorize_deployment
from agent_remote_server.services.skills.deployment_discovery import execution_plans
from agent_remote_server.services.skills.deployment_validation import saved_plans
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass(frozen=True)
class DeploymentFileDownload:
    """
    短事务固定的清单成员，文件复制后仍须重新验证原任务。
    """

    user_id: UUID
    entry: SkillTreeEntry


class NodeDeploymentContent:
    """
    所有者和内容只能来自原绑定，不接受调用方提供路径或选择新状态。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        使用请求事务与服务端私有卷配置。

        :param session (AsyncSession): 请求事务
        :param settings (Settings): 内容卷、配额与短期租约配置
        """
        self.session, self.settings = session, settings
        self.store = PrivateObjectStore(settings.skill_storage_root)
        self.content = SkillContentService(session, self.store, settings.skill_storage_policy)
        self.runtime = SkillRuntimeRepository(session)

    async def authorize(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, lease_attempt: int
    ) -> SkillDeploymentTask:
        """
        每次请求重验原始授权并持有原用户锁到事务结束。

        :param node_id (UUID): 已认证节点
        :param task_id (UUID): 精确任务身份
        :param attempt_id (UUID): 原部署尝试
        :param lease_attempt (int): 当前领取轮次
        :return SkillDeploymentTask: 本事务仍有效的完整输入绑定
        """
        return await authorize_deployment(
            self.session, self.settings, node_id, task_id, attempt_id, lease_attempt
        )

    async def describe(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, lease_attempt: int
    ) -> SkillDeploymentContent:
        """
        只读返回原计划与物化成员，后来的 head 变化不能重写传输输入。

        :param node_id (UUID): 已认证节点
        :param task_id (UUID): 精确任务身份
        :param attempt_id (UUID): 原部署尝试
        :param lease_attempt (int): 当前领取轮次
        :return SkillDeploymentContent: 完整原始输入
        """
        binding = await self.authorize(node_id, task_id, attempt_id, lease_attempt)
        operation = await SkillLibraryRepository(self.session).operation(
            binding.user_id, binding.operation_id
        )
        assert operation is not None
        targets, entries = await SkillDeploymentRepository(self.session).rows(
            binding.user_id, binding.operation_id
        )
        plan = next(
            plan
            for plan in await execution_plans(
                self.session, saved_plans(operation, targets, entries)
            )
            if plan.account_id == binding.account_id
        )
        plan = plan.model_copy(
            update={
                "sources": tuple(
                    sorted(plan.sources, key=lambda source: (source.origin, str(source.source_id)))
                )
            }
        )
        checkpoint = await self.runtime.checkpoint(
            binding.user_id, binding.account_id, binding.checkpoint_id
        )
        assert checkpoint is not None and checkpoint.directory_epoch is not None
        members = []
        for member in await self.runtime.members(checkpoint):
            item = await self.runtime.checkpoint(
                binding.user_id, binding.account_id, member.checkpoint_id
            )
            assert item is not None and item.state_epoch is not None
            members.append(
                SkillDeploymentMember(
                    entry_name=member.entry_name,
                    state_id=member.state_id,
                    state_epoch=item.state_epoch,
                    checkpoint_id=item.id,
                )
            )
        return SkillDeploymentContent(
            **_identity(binding).model_dump(),
            directory_epoch=checkpoint.directory_epoch,
            plan=plan,
            manifest=await self.content.read_tree(binding.user_id, "state", binding.content_digest),
            items=tuple(sorted(members, key=lambda item: item.entry_name)),
        )

    async def authorize_file(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, lease_attempt: int, digest: str
    ) -> DeploymentFileDownload:
        """
        文件必须属于原完整清单，知道同用户其他摘要也不构成读取资格。

        :param node_id (UUID): 已认证节点
        :param task_id (UUID): 精确任务身份
        :param attempt_id (UUID): 原部署尝试
        :param lease_attempt (int): 当前领取轮次
        :param digest (str): 请求文件摘要
        :return DeploymentFileDownload: 本次可复制的精确普通文件
        """
        binding = await self.authorize(node_id, task_id, attempt_id, lease_attempt)
        entry = await self.content.authorize_file(
            binding.user_id, "state", binding.content_digest, digest
        )
        return DeploymentFileDownload(binding.user_id, entry)

    async def copy_authorized_file(
        self, download: DeploymentFileDownload, target: BinaryIO
    ) -> None:
        """
        在数据库锁外校验全部字节，调用方重验权限后才可对外发送。

        :param download (DeploymentFileDownload): 原短事务固定的完整成员
        :param target (BinaryIO): 私有暂存文件
        """
        try:
            await self.store.copy_file(download.user_id, download.entry, target)
        except FileNotFoundError as error:
            raise SkillContentError(
                "CONTENT_INCOMPLETE", "retained content is unavailable"
            ) from error
        except ValueError as error:
            raise SkillContentError(
                "CONTENT_INVALID", "retained content verification failed"
            ) from error

    async def renew(
        self, node_id: UUID, task_id: UUID, attempt_id: UUID, lease_attempt: int
    ) -> SkillDeploymentLease:
        """
        只延长当前仍有效的领取租约，不恢复已经过期或被替代的任务。

        :param node_id (UUID): 已认证节点
        :param task_id (UUID): 精确任务身份
        :param attempt_id (UUID): 原部署尝试
        :param lease_attempt (int): 当前领取轮次
        :return SkillDeploymentLease: 同一输入的短期续租凭据
        """
        binding = await self.authorize(node_id, task_id, attempt_id, lease_attempt)
        task = await self.runtime.task(node_id, task_id)
        assert task is not None
        now = datetime.now(UTC)
        if (
            task.lease_until is None
            or task.lease_until.replace(tzinfo=task.lease_until.tzinfo or UTC) <= now
        ):
            raise SkillContentError("DEPLOYMENT_LEASE_CHANGED", "deployment lease expired")
        duration = min(self.settings.node_task_lease_seconds, 300)
        if duration <= 0:
            raise SkillContentError("DEPLOYMENT_LEASE_UNAVAILABLE", "lease duration is invalid")
        task.lease_until = now + timedelta(seconds=duration)
        await self.session.flush()
        return SkillDeploymentLease(
            **_identity(binding).model_dump(),
            lease_attempt=lease_attempt,
            server_time=now,
            lease_until=task.lease_until,
            renew_after_milliseconds=max(1, duration * 1000 // 3),
        )


def _identity(binding: SkillDeploymentTask) -> SkillDeploymentIdentity:
    """
    从已验证数据库绑定生成完整身份，不采用请求提供的所有者或内容字段。

    :param binding (SkillDeploymentTask): 本事务已授权的原绑定
    :return SkillDeploymentIdentity: 原始完整身份
    """
    return SkillDeploymentIdentity(
        operation_id=binding.operation_id,
        attempt_id=binding.attempt_id,
        task_id=binding.task_id,
        user_id=binding.user_id,
        account_id=binding.account_id,
        node_id=binding.node_id,
        checkpoint_id=binding.checkpoint_id,
        plan_digest=binding.plan_digest,
        tree_digest=binding.content_digest,
        runtime_backend="native",
    )
