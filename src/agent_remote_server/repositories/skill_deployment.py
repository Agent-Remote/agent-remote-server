"""
只追加原始目标计划，查询始终限定认证用户和原始操作。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.repositories.skill_deployment_discovery import (
    SkillDeploymentDiscoveryRepository,
)
from agent_remote_server.schemas.skill_deployment import (
    DeploymentLibrarySource,
    SkillDeploymentPlan,
)


class SkillDeploymentRepository:
    """
    计划与配置共用调用方事务，不提供覆盖既有计划的接口。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        绑定已有用户锁和保存点中的事务。

        :param session (AsyncSession): 配置受理事务
        """
        self._session = session

    async def add(self, plan: SkillDeploymentPlan) -> None:
        """
        先保存父目标，再追加带明确来源外键的版本引用。

        :param plan (SkillDeploymentPlan): 已验证的账户完整配置投影
        """
        self._session.add(
            SkillDeploymentTarget(
                user_id=plan.user_id,
                operation_id=plan.operation_id,
                account_id=plan.account_id,
                node_id=plan.node_id,
                tool_type=plan.tool_type,
                runtime_backend=plan.runtime_backend,
                plan_digest=plan.digest(),
            )
        )
        await self._session.flush()
        for source in plan.sources:
            library = isinstance(source, DeploymentLibrarySource)
            self._session.add(
                SkillDeploymentEntry(
                    user_id=plan.user_id,
                    operation_id=plan.operation_id,
                    account_id=plan.account_id,
                    origin=source.origin,
                    source_id=source.source_id,
                    name=source.name,
                    enabled=source.enabled,
                    content_digest=source.content_digest,
                    installation_id=source.source_id if library else None,
                    installation_epoch=(
                        source.installation_epoch
                        if isinstance(source, DeploymentLibrarySource)
                        else None
                    ),
                    package_revision_id=source.revision_id if library else None,
                    local_skill_id=None if library else source.source_id,
                    local_revision_id=None if library else source.revision_id,
                )
            )
        await self._session.flush()
        await SkillDeploymentDiscoveryRepository(self._session).capture(plan)

    async def rows(
        self, user_id: UUID, operation_id: UUID
    ) -> tuple[tuple[SkillDeploymentTarget, ...], tuple[SkillDeploymentEntry, ...]]:
        """
        读取原始行，不解析当前账户、规则或上游来源。

        :param user_id (UUID): 认证用户身份
        :param operation_id (UUID): 原始受理操作
        :return tuple[tuple[SkillDeploymentTarget, ...], tuple[SkillDeploymentEntry, ...]]: 原始行
        """
        targets = tuple(
            await self._session.scalars(
                select(SkillDeploymentTarget).where(
                    SkillDeploymentTarget.user_id == user_id,
                    SkillDeploymentTarget.operation_id == operation_id,
                )
            )
        )
        entries = tuple(
            await self._session.scalars(
                select(SkillDeploymentEntry).where(
                    SkillDeploymentEntry.user_id == user_id,
                    SkillDeploymentEntry.operation_id == operation_id,
                )
            )
        )
        return targets, entries
