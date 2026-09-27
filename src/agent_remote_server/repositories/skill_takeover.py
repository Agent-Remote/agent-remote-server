"""
限定首次接管的归属、历史写入者和目录权威交换查询。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Node, NodeTask, Session, ToolAccount, User
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_takeover import SkillAccountTakeover


class SkillTakeoverRepository:
    """
    写入由调用方持有用户内容锁，所有读取刷新 ORM 缓存。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保存外层请求事务。

        :param session (AsyncSession): 异步数据库事务
        """
        self.session = session

    async def account(self, user_id: UUID, account_id: UUID) -> ToolAccount | None:
        """
        精确读取仍属于活动所有者的账户。

        :param user_id (UUID): 所有者身份
        :param account_id (UUID): 账户身份
        :return ToolAccount | None: 已授权账户
        """
        return await self.session.scalar(
            select(ToolAccount)
            .join(User, User.id == ToolAccount.user_id)
            .where(ToolAccount.id == account_id, User.id == user_id, User.status == "active")
            .execution_options(populate_existing=True)
        )

    async def node(self, node_id: UUID) -> Node | None:
        """
        刷新当前节点能力和存活时间。

        :param node_id (UUID): 账户固定节点
        :return Node | None: 当前节点记录
        """
        return await self.session.get(Node, node_id, populate_existing=True)

    async def by_key(self, user_id: UUID, key: str) -> SkillAccountTakeover | None:
        """
        同用户幂等键不能跨账户复用。

        :param user_id (UUID): 所有者身份
        :param key (str): 用户请求键
        :return SkillAccountTakeover | None: 原始接管记录
        """
        return await self.session.scalar(
            select(SkillAccountTakeover)
            .where(
                SkillAccountTakeover.user_id == user_id, SkillAccountTakeover.idempotency_key == key
            )
            .execution_options(populate_existing=True)
        )

    async def for_account(self, user_id: UUID, account_id: UUID) -> SkillAccountTakeover | None:
        """
        一个账户不能被第二个请求同时接管。

        :param user_id (UUID): 所有者身份
        :param account_id (UUID): 账户身份
        :return SkillAccountTakeover | None: 原始接管记录
        """
        return await self.session.scalar(
            select(SkillAccountTakeover)
            .where(
                SkillAccountTakeover.user_id == user_id,
                SkillAccountTakeover.account_id == account_id,
            )
            .execution_options(populate_existing=True)
        )

    async def on_node(self, node_id: UUID, takeover_id: UUID) -> SkillAccountTakeover | None:
        """
        节点不能用其他节点或已禁用用户的接管身份取得内容权限。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :return SkillAccountTakeover | None: 当前授权记录
        """
        return await self.session.scalar(
            select(SkillAccountTakeover)
            .join(User, User.id == SkillAccountTakeover.user_id)
            .where(
                SkillAccountTakeover.node_id == node_id,
                SkillAccountTakeover.id == takeover_id,
                User.status == "active",
            )
            .execution_options(populate_existing=True)
        )

    async def writer_tasks(self, user_id: UUID, account_id: UUID) -> Sequence[NodeTask]:
        """
        包含终态历史任务，任务消失或失败都不等于 Helper 中没有写入者。

        :param user_id (UUID): 所有者身份
        :param account_id (UUID): 账户身份
        :return Sequence[NodeTask]: 可能写旧目录的全部历史任务
        """
        return (
            await self.session.scalars(
                select(NodeTask)
                .where(
                    NodeTask.task_type.in_(
                        (
                            "create_tool_session",
                            "create_binding_session",
                            "import_tool_account_config",
                            "migrate_tool_account_runtime",
                        )
                    ),
                    NodeTask.payload["user_id"].as_string() == str(user_id),
                    NodeTask.payload["tool_account_id"].as_string() == str(account_id),
                )
                .order_by(NodeTask.id)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def sessions(self, user_id: UUID, account_id: UUID) -> Sequence[Session]:
        """
        会话表补齐可能缺少历史派发任务的旧资源。

        :param user_id (UUID): 所有者身份
        :param account_id (UUID): 账户身份
        :return Sequence[Session]: 全部已知会话资源
        """
        return (
            await self.session.scalars(
                select(Session)
                .where(Session.user_id == user_id, Session.tool_account_id == account_id)
                .order_by(Session.id)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def publish(self, receipt: SkillAccountTakeover, checkpoint_id: UUID) -> bool:
        """
        首次权威交换要求仍是同一 migrating 空 head 和目录纪元。

        :param receipt (SkillAccountTakeover): 已授权接管收据
        :param checkpoint_id (UUID): 同次事务的新初始完整目录
        :return bool: 是否完成唯一权威交换
        """
        statement = (
            update(AccountSkillDirectoryState)
            .where(
                AccountSkillDirectoryState.user_id == receipt.user_id,
                AccountSkillDirectoryState.account_id == receipt.account_id,
                AccountSkillDirectoryState.mode == "migrating",
                AccountSkillDirectoryState.epoch == receipt.directory_epoch,
                AccountSkillDirectoryState.head_checkpoint_id.is_(None),
            )
            .values(mode="managed_v1", head_checkpoint_id=checkpoint_id)
            .returning(AccountSkillDirectoryState.account_id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None
