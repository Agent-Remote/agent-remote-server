"""
限定迁移生命周期查询与失效范围，不触碰原始比较、选择和受理响应。
"""

from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_state import AccountSkillState


class SkillPreparationLifecycleRepository:
    """
    调用方必须持有用户写锁，并与原配置或发布共用保存点。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定已有事务，不自行提交。

        :param session (AsyncSession): 异步事务
        """
        self._session = session

    async def pending(
        self, item: SkillInstallation
    ) -> list[tuple[SkillBranchPreparation, AccountSkillState]]:
        """
        只读取该安装作为迁移目标的活跃记录，关联成员交由候选授权精确检查。

        :param item (SkillInstallation): 已锁定用户安装
        :return list[tuple[SkillBranchPreparation, AccountSkillState]]: 原尝试及固定版本目标
        """
        rows = await self._session.execute(
            select(SkillBranchPreparation, AccountSkillState)
            .join(AccountSkillState, AccountSkillState.id == SkillBranchPreparation.target_state_id)
            .where(
                SkillBranchPreparation.user_id == item.user_id,
                SkillBranchPreparation.installation_id == item.id,
                SkillBranchPreparation.status == "conflicted",
            )
            .execution_options(populate_existing=True)
        )
        return [(row, branch) for row, branch in rows]

    async def supersede_success(self, user_id: UUID, success_id: UUID) -> int:
        """
        同方向同纪元成功可关联尚无替代的旧计划，重算父记录留给唯一替代 CAS。

        :param user_id (UUID): 所有者
        :param success_id (UUID): 已在同事务保存的完整成功记录
        :return int: 本次关联替代的旧计划数量
        """
        success = await self._session.scalar(
            select(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == user_id,
                SkillBranchPreparation.id == success_id,
                SkillBranchPreparation.status == "ready",
                SkillBranchPreparation.migration_sequence.is_not(None),
            )
            .execution_options(populate_existing=True)
        )
        if success is None:
            return 0
        excluded = [success.id]
        if success.recomputed_from_id is not None:
            excluded.append(success.recomputed_from_id)
        result = await self._session.scalars(
            update(SkillBranchPreparation)
            .where(
                SkillBranchPreparation.user_id == success.user_id,
                SkillBranchPreparation.account_id == success.account_id,
                SkillBranchPreparation.installation_id == success.installation_id,
                SkillBranchPreparation.installation_epoch == success.installation_epoch,
                SkillBranchPreparation.source_state_id == success.source_state_id,
                SkillBranchPreparation.target_state_id == success.target_state_id,
                SkillBranchPreparation.source_epoch == success.source_epoch,
                SkillBranchPreparation.target_epoch == success.target_epoch,
                SkillBranchPreparation.directory_epoch == success.directory_epoch,
                SkillBranchPreparation.mode.in_(("forward", "incremental")),
                or_(
                    SkillBranchPreparation.status == "conflicted",
                    and_(
                        SkillBranchPreparation.status == "superseded",
                        SkillBranchPreparation.replacement_id.is_(None),
                        SkillBranchPreparation.superseded_reason.in_(
                            ("state_reset", "state_restore")
                        ),
                    ),
                ),
                SkillBranchPreparation.id.not_in(excluded),
            )
            .values(
                status="superseded",
                superseded_reason="migration_baseline_changed",
                replacement_id=success.id,
            )
            .returning(SkillBranchPreparation.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())
