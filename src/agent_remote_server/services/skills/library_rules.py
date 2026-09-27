"""
按字段修改用户、工具和账户覆盖，保留其他字段及运行状态。
"""

from agent_remote_server.models.skill_library import SkillAccountOverride, SkillToolOverride
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import LibraryChange, LibraryContext


async def change_rules(context: LibraryContext, request: SkillRuleRequest) -> LibraryChange:
    """
    更新指定范围的字段，all-scopes 只清除启用覆盖。

    :param context (LibraryContext): 已锁定用户库上下文
    :param request (SkillRuleRequest): 已通过组合校验的规则命令
    :return LibraryChange: 实际变更和被保留覆盖的说明
    """
    account = await context.validate_scope(request.scope)
    item = await context.resolve_source(request.skill, request.scope)
    if isinstance(item, AccountLocalSkill):
        return change_local_rule(item, request)
    if item.removed:
        raise SkillContentError("SKILL_REMOVED", "skill is uninstalled")
    tools, accounts = await context.repository.rules(item)
    change = LibraryChange(skills=[item], scope=request.scope)
    revision = None
    if request.revision is not None:
        revision = await context.require_revision(item, request.revision)
        change.revisions.append(revision)
    if request.scope.is_user:
        enabled = request.command == "enable"
        change.changed = item.default_enabled != enabled
        item.default_enabled = enabled
        existing: list[SkillToolOverride | SkillAccountOverride] = [*tools, *accounts]
        for row in existing:
            if request.all_scopes:
                change.changed = change.changed or row.enabled is not None
                row.enabled = None
            elif row.enabled is not None:
                target = (
                    str(row.account_id) if isinstance(row, SkillAccountOverride) else row.tool_type
                )
                change.warnings.append(
                    f"explicit enabled override retained: {target}={row.enabled}"
                )
        return change
    selected: list[SkillToolOverride | SkillAccountOverride] = []
    for tool in request.scope.tools:
        tool_row = next((row for row in tools if row.tool_type == tool), None)
        if tool_row is None:
            tool_row = SkillToolOverride(
                user_id=context.user_id,
                installation_id=item.id,
                tool_type=tool,
                enabled=None,
                revision_id=None,
            )
            context.repository.add(tool_row)
        selected.append(tool_row)
    if account is not None:
        account_row = next((row for row in accounts if row.account_id == account.id), None)
        if account_row is None:
            account_row = SkillAccountOverride(
                user_id=context.user_id,
                installation_id=item.id,
                account_id=account.id,
                tool_type=account.tool_type,
                enabled=None,
                revision_id=None,
            )
            context.repository.add(account_row)
        selected.append(account_row)
    for row in selected:
        previous = (row.enabled, row.revision_id)
        match request.command:
            case "enable" | "disable":
                row.enabled = request.command == "enable"
            case "pin":
                assert revision is not None
                row.revision_id = revision.id
            case "unpin":
                row.revision_id = None
            case "inherit":
                if request.field in {"enabled", "all"}:
                    row.enabled = None
                if request.field in {"revision", "all"}:
                    row.revision_id = None
        change.changed = change.changed or previous != (row.enabled, row.revision_id)
    return change


def change_local_rule(item: AccountLocalSkill, request: SkillRuleRequest) -> LibraryChange:
    """
    本地启用继承恢复账户默认值，不修改运行分支或版本。

    :param item (AccountLocalSkill): 精确账户的本地来源
    :param request (SkillRuleRequest): 已授权规则请求
    :return LibraryChange: 限定账户的配置结果
    """
    if item.status != "active":
        raise SkillContentError("SKILL_REMOVED", "local skill is removed")
    if request.scope.account_id != item.account_id or request.all_scopes:
        raise SkillContentError("LOCAL_SKILL_SCOPE_REQUIRED", "local skill requires its account")
    if request.command not in {"enable", "disable", "inherit"} or (
        request.command == "inherit" and request.field == "revision"
    ):
        raise SkillContentError(
            "LOCAL_SKILL_COMMAND_UNSUPPORTED", "use state restore for local history"
        )
    enabled = request.command != "disable"
    change = LibraryChange(
        local_skills=[item], scope=request.scope, changed=item.enabled != enabled
    )
    item.enabled = enabled
    return change
