"""
只读解析账户当前有效来源和精确分支，为查询及状态变更共享前置条件。
"""

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule, SkillRuleOverride
from agent_remote_server.schemas.skill_state_commands import (
    SkillCurrentStateView,
    SkillStatePrecondition,
    SkillStateSelector,
    SkillStateTarget,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.services.tool_registry import ToolRegistry
from agent_remote_server.skill_manager.rules import resolve_skill_rule


@dataclass
class StateSelection:
    """
    不借用会初始化分支的快照预约路径，预览始终无持久化副作用。
    """

    queries: SkillStateQueryService
    local: SkillLocalRepository

    async def current(self, user_id: UUID, selector: SkillStateSelector) -> SkillCurrentStateView:
        """
        固定规则解析与全部选中分支，即使分支过期或尚待迁移也如实返回。

        :param user_id (UUID): 当前用户
        :param selector (SkillStateSelector): 明确范围
        :return SkillCurrentStateView: 可供下一次命令比较的精确选择
        """
        queries = self.queries
        skill_id = await queries.scope(user_id, selector.account_id, selector.scope, selector.skill)
        account = await queries.library.account(user_id, selector.account_id)
        assert account is not None
        if account.tool_type not in ToolRegistry.supported_tool_types():
            raise SkillContentError("UNSUPPORTED_TOOL", "account tool adapter is not registered")
        targets = []
        if skill_id is not None:
            library = await queries.library.installation(user_id, str(skill_id))
            if library is not None:
                if library.removed:
                    raise SkillContentError("SKILL_REMOVED", "selected source is removed")
                targets.append(await self._library(account, library))
            else:
                local = await queries.repository.local_source(user_id, account.id, str(skill_id))
                assert local is not None
                if local.status != "active":
                    raise SkillContentError("SKILL_REMOVED", "selected local source is not active")
                targets.append(await self._local(account, local))
        else:
            for library in await queries.library.list_installations(user_id):
                target = await self._library(account, library)
                if target.rule.included:
                    targets.append(target)
            for local in await self.local.active(user_id, account.id):
                targets.append(await self._local(account, local))
        if len({target.name for target in targets}) != len(targets):
            raise SkillContentError("SKILL_SOURCE_CONFLICT", "effective source names collide")
        directory = await queries.runtime.directory(user_id, account.id)
        return SkillCurrentStateView(
            selector=selector,
            precondition=SkillStatePrecondition(
                library_generation=await queries.library.generation(user_id),
                directory_mode=cast(Literal["legacy", "migrating", "managed_v1"], directory.mode)
                if directory
                else "legacy",
                directory_epoch=directory.epoch if directory else None,
                directory_head_id=directory.head_checkpoint_id if directory else None,
                targets=tuple(sorted(targets, key=lambda item: item.name)),
            ),
        )

    async def _library(self, account: ToolAccount, item: SkillInstallation) -> SkillStateTarget:
        """
        两个覆盖字段独立解析，不把停用误当成失去选定版本。

        :param account (ToolAccount): 已授权账户
        :param item (SkillInstallation): 已授权活动库来源
        :return SkillStateTarget: 当前规则固定的版本分支
        """
        tools, accounts = await self.queries.library.rules(item)
        tool = next(
            (
                SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
                for row in tools
                if row.tool_type == account.tool_type
            ),
            None,
        )
        override = next(
            (
                SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
                for row in accounts
                if row.account_id == account.id
            ),
            None,
        )
        assert item.default_revision_id is not None
        rule = resolve_skill_rule(item.default_enabled, item.default_revision_id, tool, override)
        branch = await self.queries.runtime.branch(
            account.user_id, account.id, item.id, item.epoch, rule.revision_id
        )
        return _target(item.name, item.id, "user_library", item.epoch, rule, branch)

    async def _local(self, account: ToolAccount, item: AccountLocalSkill) -> SkillStateTarget:
        """
        本地来源使用自己的启用值和初始版本，不套用用户库覆盖。

        :param account (ToolAccount): 已授权账户
        :param item (AccountLocalSkill): 活动本地来源
        :return SkillStateTarget: 当前本地版本的精确分支
        """
        assert item.default_revision_id is not None
        rule = ResolvedSkillRule(
            enabled=item.enabled,
            revision_id=item.default_revision_id,
            enabled_source="account",
            revision_source="account",
            eligible=True,
            included=item.enabled,
            exclusion_reason=None,
        )
        branch = await self.local.branch(
            account.user_id, account.id, item.id, item.default_revision_id
        )
        return _target(item.name, item.id, "account_local", 1, rule, branch)


def _target(
    name: str,
    skill_id: UUID,
    origin: Literal["user_library", "account_local"],
    epoch: int,
    rule: ResolvedSkillRule,
    branch: AccountSkillState | None,
) -> SkillStateTarget:
    """
    缺失分支保持空值，不能在只读选择时创建干净状态。

    :param name (str): 稳定目录名称
    :param skill_id (UUID): 来源身份
    :param origin (Literal["user_library", "account_local"]): 库或本地来源种类
    :param epoch (int): 安装纪元
    :param rule (ResolvedSkillRule): 已解析规则
    :param branch (AccountSkillState | None): 精确分支或尚未初始化
    :return SkillStateTarget: 完整比较目标
    """
    return SkillStateTarget(
        name=name,
        skill_id=skill_id,
        origin=origin,
        revision_id=rule.revision_id,
        installation_epoch=epoch,
        state_id=branch.id if branch else None,
        state_epoch=branch.epoch if branch else None,
        head_checkpoint_id=branch.head_checkpoint_id if branch else None,
        expired=branch.expired if branch else False,
        rule=rule,
    )
