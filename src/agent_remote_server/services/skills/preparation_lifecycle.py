"""
在库配置事务内按各账户真实有效目标取消旧准备，保留显式增量方向。
"""

from dataclasses import dataclass

from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_preparation_lifecycle import (
    SkillPreparationLifecycleRepository,
)
from agent_remote_server.schemas.skill_rules import SkillRuleOverride
from agent_remote_server.skill_manager.rules import resolve_skill_rule


@dataclass
class PreparationLifecycle:
    """
    只失效固定目标，不生成新比较或改变任何运行分支。
    """

    library: SkillLibraryRepository
    preparations: SkillPreparationRepository
    lifecycle: SkillPreparationLifecycleRepository

    async def library_changed(self, item: SkillInstallation) -> None:
        """
        安装身份优先于规则，独立账户覆盖不导致其他仍有效准备失效。

        :param item (SkillInstallation): 本事务实际变更的安装
        """
        tools, accounts = await self.library.rules(item)
        tool_rules = {
            row.tool_type: SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
            for row in tools
        }
        account_rules = {
            row.account_id: SkillRuleOverride(enabled=row.enabled, revision_id=row.revision_id)
            for row in accounts
        }
        assert item.default_revision_id is not None
        for row, target in await self.lifecycle.pending(item):
            reason = None
            if item.removed:
                reason = "installation_removed"
            elif row.installation_epoch != item.epoch:
                reason = "installation_epoch_changed"
            elif row.mode != "incremental":
                account = await self.library.account(item.user_id, row.account_id)
                assert account is not None
                effective = resolve_skill_rule(
                    item.default_enabled,
                    item.default_revision_id,
                    tool_rules.get(account.tool_type),
                    account_rules.get(account.id),
                )
                if not effective.included or effective.revision_id != target.base_revision_id:
                    reason = "effective_target_changed"
            if reason is not None:
                await self.preparations.replace_attempt(row, reason, None)
