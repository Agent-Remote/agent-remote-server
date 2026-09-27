"""
查询原目标发现边界及接管初始本地版本，不读取后来有效选择。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment_discovery import (
    SkillDeploymentDiscoveredSource,
    SkillDeploymentDiscovery,
)
from agent_remote_server.models.skill_local import AccountLocalSkill, AccountLocalSkillRevision
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_deployment import DeploymentLocalSource, SkillDeploymentPlan

type DiscoveryRows = tuple[
    tuple[SkillDeploymentDiscovery, ...], tuple[SkillDeploymentDiscoveredSource, ...]
]


class SkillDeploymentDiscoveryRepository:
    """
    共用原用户事务，历史记录只允许从待发现推进为固定解析。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保留外层受理或接管事务。

        :param session (AsyncSession): 已持有用户锁的事务
        """
        self.session = session

    async def capture(self, plan: SkillDeploymentPlan) -> None:
        """
        仅新受理的未接管绑定目标保存预期纪元，已管理账户不补发发现许可。

        :param plan (SkillDeploymentPlan): 本次刚保存的原始计划
        """
        if plan.node_id is None:
            return
        directory = await self.session.scalar(
            select(AccountSkillDirectoryState).where(
                AccountSkillDirectoryState.user_id == plan.user_id,
                AccountSkillDirectoryState.account_id == plan.account_id,
            )
        )
        if directory is not None and directory.mode == "managed_v1":
            return
        if directory is not None and directory.mode not in {"legacy", "migrating"}:
            raise ValueError("unknown deployment discovery directory mode")
        epoch = 1 if directory is None else directory.epoch + (directory.mode == "legacy")
        self.session.add(
            SkillDeploymentDiscovery(
                user_id=plan.user_id,
                operation_id=plan.operation_id,
                account_id=plan.account_id,
                original_digest=plan.digest(),
                directory_epoch=epoch,
            )
        )
        await self.session.flush()

    async def rows(self, user_id: UUID, operation_id: UUID) -> DiscoveryRows:
        """
        返回同用户同原操作的全部发现证据。

        :param user_id (UUID): 原始所有者
        :param operation_id (UUID): 原配置操作
        :return DiscoveryRows: 边界及补充来源
        """
        boundaries = tuple(
            await self.session.scalars(
                select(SkillDeploymentDiscovery)
                .where(
                    SkillDeploymentDiscovery.user_id == user_id,
                    SkillDeploymentDiscovery.operation_id == operation_id,
                )
                .execution_options(populate_existing=True)
            )
        )
        sources = tuple(
            await self.session.scalars(
                select(SkillDeploymentDiscoveredSource).where(
                    SkillDeploymentDiscoveredSource.user_id == user_id,
                    SkillDeploymentDiscoveredSource.operation_id == operation_id,
                )
            )
        )
        return boundaries, sources

    async def receipts(
        self, boundaries: Sequence[SkillDeploymentDiscovery]
    ) -> tuple[SkillAccountTakeover, ...]:
        """
        读取已解析边界明确引用的原接管，不查找后来账户状态。

        :param boundaries (Sequence[SkillDeploymentDiscovery]): 已归属原操作的边界
        :return tuple[SkillAccountTakeover, ...]: 原接管元数据
        """
        identities = [row.takeover_id for row in boundaries if row.takeover_id is not None]
        if not identities:
            return ()
        return tuple(
            await self.session.scalars(
                select(SkillAccountTakeover).where(
                    SkillAccountTakeover.user_id == boundaries[0].user_id,
                    SkillAccountTakeover.id.in_(identities),
                )
            )
        )

    async def pending(self, receipt: SkillAccountTakeover) -> tuple[SkillDeploymentDiscovery, ...]:
        """
        只读取原账户等待同一次首次目录纪元的受理边界。

        :param receipt (SkillAccountTakeover): 即将提交的原始接管
        :return tuple[SkillDeploymentDiscovery, ...]: 待原子解析的边界
        """
        return tuple(
            await self.session.scalars(
                select(SkillDeploymentDiscovery)
                .where(
                    SkillDeploymentDiscovery.user_id == receipt.user_id,
                    SkillDeploymentDiscovery.account_id == receipt.account_id,
                    SkillDeploymentDiscovery.directory_epoch == receipt.directory_epoch,
                    SkillDeploymentDiscovery.takeover_id.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        )

    async def initial_sources(
        self, receipt: SkillAccountTakeover
    ) -> tuple[DeploymentLocalSource, ...]:
        """
        只读取初始目录登记的第一版本，停用和后续默认版本不参与原始解析。

        :param receipt (SkillAccountTakeover): 已提交接管身份
        :return tuple[DeploymentLocalSource, ...]: 精确初始手工来源
        """
        rows = (
            await self.session.execute(
                select(AccountLocalSkill, AccountLocalSkillRevision)
                .join(
                    AccountLocalSkillRevision,
                    (AccountLocalSkillRevision.user_id == AccountLocalSkill.user_id)
                    & (AccountLocalSkillRevision.account_id == AccountLocalSkill.account_id)
                    & (AccountLocalSkillRevision.local_skill_id == AccountLocalSkill.id)
                    & (AccountLocalSkillRevision.number == 1),
                )
                .where(
                    AccountLocalSkill.user_id == receipt.user_id,
                    AccountLocalSkill.account_id == receipt.account_id,
                    AccountLocalSkill.source_checkpoint_id == receipt.checkpoint_id,
                )
            )
        ).all()
        return tuple(
            DeploymentLocalSource(
                source_id=item.id,
                revision_id=revision.id,
                content_digest=revision.content_digest,
                name=item.name,
                enabled=True,
            )
            for item, revision in rows
        )
