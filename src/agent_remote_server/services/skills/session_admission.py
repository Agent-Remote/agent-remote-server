"""
为原会话入口提供用户锁内运行模式、全账户准备和精确快照预约。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, Session, ToolAccount
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector
from agent_remote_server.services.skills.account_preparation import AccountSkillPreparation
from agent_remote_server.services.skills.snapshots import SkillSnapshotService
from agent_remote_server.services.skills.takeover_admission import (
    SkillTakeoverAdmission,
    SkillTakeoverPending,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass(frozen=True)
class SkillAdmissionPending:
    """
    正常迁移冲突需要提交原输入，但绝不接受一个缺少技能的 session。
    """

    account_id: UUID
    migration_ids: tuple[UUID, ...]


class SkillSessionAdmission:
    """
    共享启动保存点，数据库提交与用户错误映射归外层会话服务。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        使用部署私有卷和额度，不接受客户端提供路径或系统版本。

        :param session (AsyncSession): 外层会话事务
        :param settings (Settings): 部署配置
        """
        self.settings = settings
        self.takeover = SkillTakeoverAdmission(session, settings)
        store = PrivateObjectStore(settings.skill_storage_root)
        self.preparation = AccountSkillPreparation(session, store, settings.skill_storage_policy)
        self.snapshots = SkillSnapshotService(session, store, settings.skill_storage_policy)

    async def required(self, account: ToolAccount) -> bool:
        """
        从模式判断一直持锁到快照提交，避免并发启用规则绕过新会话准备。

        :param account (ToolAccount): 已授权账户
        :return bool: 是否必须使用受管路径
        """
        service = self.preparation.service
        await service.queries.library.lock_library(account.user_id)
        current = await service.selection.current(
            account.user_id, SkillStateSelector(account_id=account.id, scope="account-directory")
        )
        needed = current.precondition.directory_mode != "legacy" or any(
            target.rule.included for target in current.precondition.targets
        )
        if needed and not self.settings.skill_manager_enabled:
            raise ApiError(
                code="SKILL_MANAGER_DISABLED",
                message="Managed skill admission is disabled.",
                status_code=409,
            )
        return needed

    async def prepare(
        self, account: ToolAccount, node: Node
    ) -> SkillAdmissionPending | SkillTakeoverPending | None:
        """
        保留全部正常冲突，未接管账户绝不能直接从空目录开始。

        :param account (ToolAccount): 已授权受管账户
        :param node (Node): 已选择的兼容节点
        :return SkillAdmissionPending | SkillTakeoverPending | None: 待保存预约或迁移，已就绪为空
        """
        takeover = await self.takeover.ensure(account, node)
        if takeover is not None:
            return takeover
        conflicts = await self.preparation.prepare(account.user_id, account.id)
        return SkillAdmissionPending(account.id, conflicts) if conflicts else None

    async def reserve(self, session: Session, task: NodeTask, node: Node) -> None:
        """
        固定系统引用与精确任务，任务仅携带快照指针而不带私有清单。

        :param session (Session): 新建启动中会话
        :param task (NodeTask): 本次精确准备任务
        :param node (Node): 已核对能力的固定节点
        """
        references: dict[str, object] = {
            "ego-browser": {
                "version": self.settings.ego_browser_expected_skill_version,
                "commit": self.settings.ego_browser_expected_skill_commit,
                "tree_sha256": self.settings.ego_browser_expected_skill_tree_sha256,
            }
        }
        if session.device_control_protocol_version is not None:
            references["agent-remote-device"] = {
                "node_release_version": node.version,
                "protocol_version": session.device_control_protocol_version,
            }
        snapshot = await self.snapshots.reserve(session.user_id, session.id, task.id, references)
        task.payload = {
            **task.payload,
            "skill_manager": {
                "protocol_version": 1,
                "manifest_version": 1,
                "snapshot_id": str(snapshot.id),
                "task_id": str(task.id),
            },
        }
