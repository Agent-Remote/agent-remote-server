"""
提供用户技能库的显式归属查询和版本历史访问。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.db import Base
from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_library import (
    SkillAccountOverride,
    SkillActivation,
    SkillInstallation,
    SkillInstallationEpoch,
    SkillLibrary,
    SkillOperation,
    SkillRevision,
    SkillSourceObservation,
    SkillToolOverride,
)
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository


class SkillLibraryRepository:
    """
    复用存储用户锁，让配置引用与内容回收遵守同一锁顺序。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定调用方事务。

        :param session (AsyncSession): 异步数据库事务
        """
        self._session = session
        self.storage = SkillStorageRepository(session)
        self.local = SkillLocalRepository(session)

    async def lock_library(self, user_id: UUID) -> SkillLibrary:
        """
        首先锁定用户存储，再读写用户库代数。

        :param user_id (UUID): 认证用户标识
        :return SkillLibrary: 当前用户已锁定的库记录
        """
        await self.storage.lock_usage(user_id)
        library = await self._session.get(SkillLibrary, user_id, populate_existing=True)
        if library is None:
            library = SkillLibrary(user_id=user_id, generation=0)
            self._session.add(library)
            await self._session.flush()
        return library

    async def read_library(self, user_id: UUID) -> SkillLibrary | None:
        """
        对已有库取得一致读锁，新用户查询不创建库或改变代数。

        :param user_id (UUID): 认证用户标识
        :return SkillLibrary | None: 已存在的用户库
        """
        usage = await self.storage.lock_existing_usage(user_id)
        if usage is None:
            return None
        return await self._session.get(SkillLibrary, user_id, populate_existing=True)

    async def generation(self, user_id: UUID) -> int:
        """
        只读查询尚未建库的用户也返回零代数。

        :param user_id (UUID): 认证用户标识
        :return int: 当前配置代数
        """
        value = await self._session.scalar(
            select(SkillLibrary.generation).where(SkillLibrary.user_id == user_id)
        )
        return value or 0

    async def installation(self, user_id: UUID, identifier: str) -> SkillInstallation | None:
        """
        名称仅查当前安装，稳定标识可读取同用户归档记录。

        :param user_id (UUID): 认证用户标识
        :param identifier (str): 用户库名称或稳定标识
        :return SkillInstallation | None: 当前用户的条目
        """
        query = select(SkillInstallation).where(SkillInstallation.user_id == user_id)
        try:
            identity = UUID(identifier)
        except ValueError:
            query = query.where(
                SkillInstallation.name == identifier, SkillInstallation.removed.is_(False)
            )
        else:
            query = query.where(SkillInstallation.id == identity)
        return await self._session.scalar(query.execution_options(populate_existing=True))

    async def source(self, user_id: UUID, source_key: str) -> SkillInstallation | None:
        """
        通过来源身份查找可重新安装的稳定记录。

        :param user_id (UUID): 认证用户标识
        :param source_key (str): 来源和子路径摘要
        :return SkillInstallation | None: 当前或已归档的同来源条目
        """
        return await self._session.scalar(
            select(SkillInstallation).where(
                SkillInstallation.user_id == user_id, SkillInstallation.source_key == source_key
            )
        )

    async def list_installations(self, user_id: UUID) -> Sequence[SkillInstallation]:
        """
        列出用户当前已安装条目。

        :param user_id (UUID): 认证用户标识
        :return Sequence[SkillInstallation]: 按名称排列的当前条目
        """
        return (
            await self._session.scalars(
                select(SkillInstallation)
                .where(SkillInstallation.user_id == user_id, SkillInstallation.removed.is_(False))
                .order_by(SkillInstallation.name)
                .execution_options(populate_existing=True)
            )
        ).all()

    async def revision(self, user_id: UUID, skill_id: UUID, selector: str) -> SkillRevision | None:
        """
        版本选择始终限制在同用户同技能，支持稳定 UUID 和本库编号。

        :param user_id (UUID): 用户标识
        :param skill_id (UUID): 稳定技能标识
        :param selector (str): 版本 UUID 或 r 编号
        :return SkillRevision | None: 匹配的版本记录
        """
        query = select(SkillRevision).where(
            SkillRevision.user_id == user_id, SkillRevision.installation_id == skill_id
        )
        if selector.startswith("r") and selector[1:].isdigit():
            number = int(selector[1:])
            if number > 2**63 - 1:
                return None
            query = query.where(SkillRevision.number == number)
        else:
            try:
                identity = UUID(selector)
            except ValueError:
                return None
            query = query.where(SkillRevision.id == identity)
        return await self._session.scalar(query.execution_options(populate_existing=True))

    async def content_revision(
        self, user_id: UUID, skill_id: UUID, digest: str
    ) -> SkillRevision | None:
        """
        查找相同内容的既有版本，避免创建重复内容编号。

        :param user_id (UUID): 用户标识
        :param skill_id (UUID): 技能标识
        :param digest (str): 完整树身份
        :return SkillRevision | None: 已登记内容版本
        """
        return await self._session.scalar(
            select(SkillRevision).where(
                SkillRevision.user_id == user_id,
                SkillRevision.installation_id == skill_id,
                SkillRevision.content_digest == digest,
            )
        )

    async def revisions(self, user_id: UUID, skill_id: UUID) -> Sequence[SkillRevision]:
        """
        查询条目全部版本及过期墓碑。

        :param user_id (UUID): 用户标识
        :param skill_id (UUID): 技能标识
        :return Sequence[SkillRevision]: 按登记编号排列的版本
        """
        return (
            await self._session.scalars(
                select(SkillRevision)
                .where(SkillRevision.user_id == user_id, SkillRevision.installation_id == skill_id)
                .order_by(SkillRevision.number)
            )
        ).all()

    async def next_revision_number(self, user_id: UUID, skill_id: UUID) -> int:
        """
        在用户锁内分配单项下一个内容版本编号。

        :param user_id (UUID): 用户标识
        :param skill_id (UUID): 技能标识
        :return int: 单调递增的版本编号
        """
        maximum = await self._session.scalar(
            select(func.max(SkillRevision.number)).where(
                SkillRevision.user_id == user_id, SkillRevision.installation_id == skill_id
            )
        )
        return (maximum or 0) + 1

    async def previous_activation(self, item: SkillInstallation) -> SkillRevision | None:
        """
        选择激活历史中最近的不同版本，候选版本不会参与。

        :param item (SkillInstallation): 当前用户已授权安装
        :return SkillRevision | None: 上一次不同版本
        """
        return await self._session.scalar(
            select(SkillRevision)
            .join(SkillActivation, SkillActivation.revision_id == SkillRevision.id)
            .where(
                SkillActivation.user_id == item.user_id,
                SkillActivation.installation_id == item.id,
                SkillActivation.revision_id != item.default_revision_id,
            )
            .order_by(SkillActivation.generation.desc())
            .limit(1)
        )

    async def rules(
        self, item: SkillInstallation
    ) -> tuple[Sequence[SkillToolOverride], Sequence[SkillAccountOverride]]:
        """
        查询当前技能全部工具及账户覆盖，保留空字段的继承语义。

        :param item (SkillInstallation): 已授权安装
        :return tuple[Sequence[SkillToolOverride], Sequence[SkillAccountOverride]]: 工具与账户覆盖
        """
        tools = (
            await self._session.scalars(
                select(SkillToolOverride)
                .where(
                    SkillToolOverride.user_id == item.user_id,
                    SkillToolOverride.installation_id == item.id,
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        accounts = (
            await self._session.scalars(
                select(SkillAccountOverride)
                .where(
                    SkillAccountOverride.user_id == item.user_id,
                    SkillAccountOverride.installation_id == item.id,
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        return tools, accounts

    async def account(self, user_id: UUID, account_id: UUID) -> ToolAccount | None:
        """
        严格限制账户归属，不借已知账户 ID 探测其他用户。

        :param user_id (UUID): 认证用户标识
        :param account_id (UUID): 账户标识
        :return ToolAccount | None: 同用户账户
        """
        return await self._session.scalar(
            select(ToolAccount)
            .where(ToolAccount.user_id == user_id, ToolAccount.id == account_id)
            .execution_options(populate_existing=True)
        )

    async def accounts(self, user_id: UUID) -> Sequence[ToolAccount]:
        """
        查询受用户默认变更影响的自有账户。

        :param user_id (UUID): 认证用户标识
        :return Sequence[ToolAccount]: 当前用户账户
        """
        return (
            await self._session.scalars(select(ToolAccount).where(ToolAccount.user_id == user_id))
        ).all()

    async def delete_account_overrides(self, user_id: UUID, account_id: UUID) -> bool:
        """
        只清理已通过账户删除条件检查的自有账户覆盖。

        :param user_id (UUID): 认证用户标识
        :param account_id (UUID): 待删除账户
        :return bool: 是否删除了至少一条配置规则
        """
        result = await self._session.scalars(
            delete(SkillAccountOverride)
            .where(
                SkillAccountOverride.user_id == user_id,
                SkillAccountOverride.account_id == account_id,
            )
            .returning(SkillAccountOverride.installation_id)
        )
        return result.first() is not None

    async def epoch(self, item: SkillInstallation) -> SkillInstallationEpoch:
        """
        取得当前安装纪元供卸载归档。

        :param item (SkillInstallation): 已授权安装
        :return SkillInstallationEpoch: 当前纪元
        """
        row = await self._session.get(SkillInstallationEpoch, (item.user_id, item.id, item.epoch))
        assert row is not None
        return row

    async def observations(self, item: SkillInstallation) -> Sequence[SkillSourceObservation]:
        """
        查询用于检测固定标签漂移的独立观测记录。

        :param item (SkillInstallation): 已授权安装
        :return Sequence[SkillSourceObservation]: 来源历史
        """
        return (
            await self._session.scalars(
                select(SkillSourceObservation).where(
                    SkillSourceObservation.user_id == item.user_id,
                    SkillSourceObservation.installation_id == item.id,
                )
            )
        ).all()

    async def operation_by_key(self, user_id: UUID, key: str) -> SkillOperation | None:
        """
        获取用户的原始幂等操作结果。

        :param user_id (UUID): 用户标识
        :param key (str): 幂等键
        :return SkillOperation | None: 原始受理结果
        """
        return await self._session.scalar(
            select(SkillOperation)
            .where(SkillOperation.user_id == user_id, SkillOperation.idempotency_key == key)
            .execution_options(populate_existing=True)
        )

    async def operation(self, user_id: UUID, operation_id: UUID) -> SkillOperation | None:
        """
        通过稳定操作 ID 查询当前用户操作。

        :param user_id (UUID): 用户标识
        :param operation_id (UUID): 操作标识
        :return SkillOperation | None: 同用户操作
        """
        return await self._session.scalar(
            select(SkillOperation)
            .where(SkillOperation.user_id == user_id, SkillOperation.id == operation_id)
            .execution_options(populate_existing=True)
        )

    def add(self, row: Base) -> None:
        """
        将已经过服务验证的实体加入同一事务。

        :param row (Base): 已验证持久化实体
        """
        self._session.add(row)

    async def flush(self) -> None:
        """
        验证数据库约束，但不提前提交外层事务。
        """
        await self._session.flush()
