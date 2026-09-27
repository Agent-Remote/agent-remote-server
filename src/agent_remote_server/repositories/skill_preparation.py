"""
管理有效分支使用顺序与独立迁移受理记录。
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_preparation import (
    SkillBranchPreparation,
    SkillEffectiveBranch,
)
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshotItem
from agent_remote_server.models.skill_state import AccountSkillState


class SkillPreparationRepository:
    """
    调用方持有用户内容锁，所有引用与外层事务一起提交。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定请求事务。

        :param session (AsyncSession): 异步数据库事务
        """
        self._session = session

    async def effective(
        self, user_id: UUID, account_id: UUID, installation_id: UUID, epoch: int
    ) -> AccountSkillState | None:
        """
        读取最后成功预约的分支当前 head，不能按最近写入时间选择来源。

        :param user_id (UUID): 所有者
        :param account_id (UUID): 账户身份
        :param installation_id (UUID): 稳定来源
        :param epoch (int): 当前安装纪元
        :return AccountSkillState | None: 真实有效分支或无历史
        """
        return await self._session.scalar(
            select(AccountSkillState)
            .join(SkillEffectiveBranch, SkillEffectiveBranch.state_id == AccountSkillState.id)
            .where(
                SkillEffectiveBranch.user_id == user_id,
                SkillEffectiveBranch.account_id == account_id,
                SkillEffectiveBranch.installation_id == installation_id,
                SkillEffectiveBranch.installation_epoch == epoch,
            )
            .execution_options(populate_existing=True)
        )

    async def has_unrecorded_use(
        self, user_id: UUID, account_id: UUID, installation_id: UUID, epoch: int
    ) -> bool:
        """
        区分旧部署缺失使用顺序和用户仅重置过但从未实际预约的分支。

        :param user_id (UUID): 所有者
        :param account_id (UUID): 账户身份
        :param installation_id (UUID): 稳定来源
        :param epoch (int): 当前安装纪元
        :return bool: 是否有真实历史使用但缺少可信顺序
        """
        return (
            await self._session.scalar(
                select(SessionSkillSnapshotItem.snapshot_id)
                .join(AccountSkillState, AccountSkillState.id == SessionSkillSnapshotItem.state_id)
                .where(
                    AccountSkillState.user_id == user_id,
                    AccountSkillState.account_id == account_id,
                    AccountSkillState.installation_id == installation_id,
                    AccountSkillState.installation_epoch == epoch,
                )
                .limit(1)
            )
            is not None
        )

    async def record(self, branch: AccountSkillState, snapshot_id: UUID) -> None:
        """
        仅在完整预约和成员保存成功后更新使用顺序。

        :param branch (AccountSkillState): 已发布且实际暴露的用户库分支
        :param snapshot_id (UUID): 本次精确快照
        """
        assert branch.installation_id is not None
        row = await self._session.get(
            SkillEffectiveBranch,
            (branch.account_id, branch.installation_id, branch.installation_epoch),
            populate_existing=True,
        )
        if row is None:
            row = SkillEffectiveBranch(
                user_id=branch.user_id,
                account_id=branch.account_id,
                installation_id=branch.installation_id,
                installation_epoch=branch.installation_epoch,
                state_id=branch.id,
                snapshot_id=snapshot_id,
            )
            self._session.add(row)
        else:
            row.state_id = branch.id
            row.snapshot_id = snapshot_id

    async def receipt(self, user_id: UUID, key: str) -> SkillBranchPreparation | None:
        """
        原受理结果按用户键查询，不重新执行或解释今天的规则。

        :param user_id (UUID): 已认证用户
        :param key (str): 持久化幂等键
        :return SkillBranchPreparation | None: 原始受理记录
        """
        return await self._session.scalar(
            select(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == user_id,
                SkillBranchPreparation.idempotency_key == key,
            )
            .execution_options(populate_existing=True)
        )

    async def receipt_by_id(
        self, user_id: UUID, operation_id: UUID
    ) -> SkillBranchPreparation | None:
        """
        按所有者和原始操作身份读取回执，避免跨用户枚举。

        :param user_id (UUID): 已认证所有者
        :param operation_id (UUID): 原始迁移或准备身份
        :return SkillBranchPreparation | None: 可见原始记录或不存在
        """
        return await self._session.scalar(
            select(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == user_id,
                SkillBranchPreparation.id == operation_id,
            )
            .execution_options(populate_existing=True)
        )

    async def latest_migration(
        self, source: AccountSkillState, target: AccountSkillState | None, directory_epoch: int
    ) -> SkillBranchPreparation | None:
        """
        只在同一方向和全部纪元内查找完整成功记录，失败或预览绝不推进基线。

        :param source (AccountSkillState): 已授权来源分支
        :param target (AccountSkillState | None): 精确目标分支或尚未创建
        :param directory_epoch (int): 当前账户目录纪元
        :return SkillBranchPreparation | None: 最新成功迁移或无历史
        """
        if target is None:
            return None
        return await self._session.scalar(
            select(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == source.user_id,
                SkillBranchPreparation.account_id == source.account_id,
                SkillBranchPreparation.source_state_id == source.id,
                SkillBranchPreparation.target_state_id == target.id,
                SkillBranchPreparation.source_epoch == source.epoch,
                SkillBranchPreparation.target_epoch == target.epoch,
                SkillBranchPreparation.directory_epoch == directory_epoch,
                SkillBranchPreparation.status == "ready",
                SkillBranchPreparation.migration_sequence.is_not(None),
            )
            .order_by(SkillBranchPreparation.migration_sequence.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )

    async def supersede(
        self, user_id: UUID, account_id: UUID, reason: str = "state_changed"
    ) -> int:
        """
        显式状态变更只取消旧计划，绝不删除输入或应用旧选择。

        :param user_id (UUID): 所有者
        :param account_id (UUID): 已变更账户
        :param reason (str): 显式状态变更原因
        :return int: 已失效的迁移数
        """
        result = await self._session.scalars(
            update(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == user_id,
                SkillBranchPreparation.account_id == account_id,
                SkillBranchPreparation.status == "conflicted",
            )
            .values(status="superseded", superseded_reason=reason)
            .returning(SkillBranchPreparation.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())

    async def resolve(
        self,
        row: SkillBranchPreparation,
        checkpoint_id: UUID,
        directory_id: UUID,
        sequence: int | None,
    ) -> bool:
        """
        只更新仍活跃的原冲突结果与成功序号，不改写任何原始输入或回执 JSON。

        :param row (SkillBranchPreparation): 已锁定原始迁移冲突
        :param checkpoint_id (UUID): 精确目标新视图
        :param directory_id (UUID): 完整已发布目录
        :param sequence (int | None): 向前或增量迁移的成功序号，初次或旧版准备为空
        :return bool: 是否原子更新了仍活跃的原记录
        """
        return (
            await self._session.scalar(
                update(SkillBranchPreparation)
                .where(
                    SkillBranchPreparation.user_id == row.user_id,
                    SkillBranchPreparation.account_id == row.account_id,
                    SkillBranchPreparation.id == row.id,
                    SkillBranchPreparation.status == "conflicted",
                    SkillBranchPreparation.target_epoch == row.target_epoch,
                    SkillBranchPreparation.source_epoch == row.source_epoch,
                    SkillBranchPreparation.directory_epoch == row.directory_epoch,
                )
                .values(
                    status="ready",
                    result_checkpoint_id=checkpoint_id,
                    result_directory_id=directory_id,
                    migration_sequence=sequence,
                )
                .returning(SkillBranchPreparation.id)
                .execution_options(synchronize_session=False)
            )
            is not None
        )

    async def replace_attempt(
        self, row: SkillBranchPreparation, reason: str, replacement_id: UUID | None
    ) -> bool:
        """
        将活跃旧尝试标记失效并保存可追踪替代，原输入和计划保持不变。

        :param row (SkillBranchPreparation): 写锁内授权的旧尝试
        :param reason (str): 明确失效原因
        :param replacement_id (UUID | None): 新比较或已取代它的成功记录
        :return bool: 是否仍然是可失效的原活跃记录
        """
        if row.status != "conflicted" and not (
            row.status == "superseded"
            and row.replacement_id is None
            and row.superseded_reason in {"state_reset", "state_restore"}
        ):
            return False
        return (
            await self._session.scalar(
                update(SkillBranchPreparation)
                .where(
                    SkillBranchPreparation.id == row.id,
                    SkillBranchPreparation.user_id == row.user_id,
                    SkillBranchPreparation.account_id == row.account_id,
                    SkillBranchPreparation.status == row.status,
                    SkillBranchPreparation.replacement_id.is_(None),
                    SkillBranchPreparation.superseded_reason == row.superseded_reason,
                )
                .values(
                    status="superseded", superseded_reason=reason, replacement_id=replacement_id
                )
                .returning(SkillBranchPreparation.id)
                .execution_options(synchronize_session=False)
            )
            is not None
        )
