"""
在普通启动事务内预约原节点目录接管，保留没有创建会话的可重试结果。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, ToolAccount
from agent_remote_server.schemas.skill_takeover import SkillTakeoverRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.takeover import SkillAccountTakeoverService
from agent_remote_server.services.skills.takeover_context import content_hash, takeover_payload


@dataclass(frozen=True)
class SkillTakeoverPending:
    """
    只证明预约已受理，不表示进程静止、捕获完成或新会话已创建。
    """

    account_id: UUID
    takeover_id: UUID
    status: str


class SkillTakeoverAdmission:
    """
    接管只允许使用原节点与原后端，不能复用一般会话的备用节点选择。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        复用外层用户锁及保存点，数据库提交归会话服务负责。

        :param session (AsyncSession): 普通启动事务
        :param settings (Settings): 部署能力与内容配置
        """
        self.service = SkillAccountTakeoverService(session, settings)

    async def ensure(self, account: ToolAccount, node: Node) -> SkillTakeoverPending | None:
        """
        完成接管的账户继续准备，旧账户只创建或复用同一原始预约。

        :param account (ToolAccount): 已授权的活动账户
        :param node (Node): 普通准入选出的兼容节点
        :return SkillTakeoverPending | None: 待提交预约或无需接管
        """
        context = self.service.context
        await context.storage.lock_usage(account.user_id)
        directory = await context.runtime.directory(account.user_id, account.id)
        if directory is not None and directory.mode == "managed_v1":
            return None
        if account.affinity_node_id != node.id or account.runtime_backend != "native":
            raise SkillContentError(
                "SKILL_MANAGER_UNSUPPORTED", "takeover requires the original supported Native node"
            )
        await context.supported(account)
        prior = await context.repository.for_account(account.user_id, account.id)
        if prior is None:
            if directory is not None and directory.mode != "legacy":
                raise SkillContentError(
                    "TAKEOVER_RECOVERY_REQUIRED", "migrating account lacks its original reservation"
                )
            epoch = directory.epoch if directory is not None else 0
            prior = await self.service.reserve(
                account.user_id,
                account.id,
                SkillTakeoverRequest(
                    idempotency_key=f"session-takeover:{account.id}:{epoch}",
                    expected_directory_epoch=epoch,
                ),
            )
        else:
            task = await context.runtime.task(prior.node_id, prior.task_id)
            if (
                directory is None
                or directory.mode != "migrating"
                or directory.head_checkpoint_id is not None
                or directory.epoch != prior.directory_epoch
                or prior.node_id != account.affinity_node_id
                or prior.runtime_backend != account.runtime_backend
                or prior.status not in {"reserved", "uploading"}
                or task is None
                or task.status not in {"pending", "leased", "running"}
                or task.task_type != "takeover_tool_account_skills"
                or task.task_id != f"takeover_tool_account_skills:{prior.id}"
                or content_hash(task.payload) != content_hash(takeover_payload(prior))
            ):
                raise SkillContentError(
                    "TAKEOVER_RECOVERY_REQUIRED",
                    "original takeover requires recovery",
                    details={"takeover_id": str(prior.id), "account_id": str(account.id)},
                )
        return SkillTakeoverPending(account.id, prior.id, prior.status)
