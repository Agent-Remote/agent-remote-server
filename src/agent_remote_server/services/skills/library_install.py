"""
执行多项原子安装、同来源重装和无副作用的重复安装。
"""

from uuid import uuid4

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_library import (
    SkillAccountOverride,
    SkillInstallation,
    SkillInstallationEpoch,
    SkillToolOverride,
)
from agent_remote_server.schemas.skill_library import SkillAddRequest, SkillScope
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import LibraryChange, LibraryContext


async def add_skills(context: LibraryContext, request: SkillAddRequest) -> LibraryChange:
    """
    先校验全部候选和冲突，再在外层保存点内登记所有选定项。

    :param context (LibraryContext): 已锁定用户库上下文
    :param request (SkillAddRequest): 完整安装计划
    :return LibraryChange: 全部成功的安装结果
    """
    account = await context.validate_scope(request.scope)
    names = [candidate.name for candidate in request.items]
    identities = [candidate.source.identity() for candidate in request.items]
    if len(set(names)) != len(names) or len(set(identities)) != len(identities):
        raise SkillContentError("SOURCE_AMBIGUOUS", "selected skills repeat a name or source path")
    metadata = [await context.validate_package(candidate) for candidate in request.items]
    existing: list[SkillInstallation | None] = []
    for candidate in request.items:
        same = await context.repository.source(context.user_id, candidate.source.identity())
        named = await context.repository.installation(context.user_id, candidate.name)
        if named is not None and (same is None or named.id != same.id):
            raise SkillContentError(
                "SKILL_SOURCE_CONFLICT", "active name belongs to another source"
            )
        if same is not None:
            await context.validate_observation(same, candidate)
            if same.name != candidate.name:
                raise SkillContentError("SOURCE_LAYOUT_CHANGED", "source changed its skill name")
            if not same.removed:
                revision = await context.require_revision(same, str(same.default_revision_id))
                if revision.content_digest != candidate.tree_digest:
                    raise SkillContentError(
                        "UPDATE_REQUIRED", "installed source changed; use update"
                    )
            if (
                not same.removed or request.scope_explicit or not request.scope.is_user
            ) and not await scope_matches(context, same, request.scope):
                raise SkillContentError(
                    "SCOPE_CONFLICT", "use enable, disable or inherit to change existing rules"
                )
        existing.append(same)
    change = LibraryChange(scope=request.scope)
    for candidate, details, item in zip(request.items, metadata, existing, strict=True):
        if item is not None and not item.removed:
            change.skills.append(item)
            revision, _ = await context.register_revision(item, candidate, details)
            change.revisions.append(revision)
            continue
        if item is None:
            item = SkillInstallation(
                id=uuid4(),
                user_id=context.user_id,
                name=candidate.name,
                source_key=candidate.source.identity(),
                source_json=candidate.source.model_dump(mode="json"),
                tracking_json=candidate.provenance.model_dump(mode="json"),
                default_revision_id=None,
                default_enabled=request.scope.is_user,
                epoch=1,
                removed=False,
            )
            context.repository.add(item)
            await context.repository.flush()
            _initial_overrides(context, item, request.scope, account)
        else:
            item.epoch += 1
            item.removed = False
            item.tracking_json = candidate.provenance.model_dump(mode="json")
            change.warnings.append(
                "reinstalled: retained rules and published account state require epoch migration"
            )
        context.repository.add(
            SkillInstallationEpoch(
                user_id=context.user_id, installation_id=item.id, epoch=item.epoch
            )
        )
        revision, _ = await context.register_revision(item, candidate, details)
        context.activate(item, revision)
        change.skills.append(item)
        change.revisions.append(revision)
        change.changed = True
    return change


async def scope_matches(
    context: LibraryContext, item: SkillInstallation, scope: SkillScope
) -> bool:
    """
    比较启用范围，不把已有固定版本当成重复安装可重置的字段。

    :param context (LibraryContext): 已锁定用户上下文
    :param item (SkillInstallation): 原有安装
    :param scope (SkillScope): 请求启用范围
    :return bool: 是否与既有启用规则完全一致
    """
    tools, accounts = await context.repository.rules(item)
    actual_tools = {row.tool_type: row.enabled for row in tools if row.enabled is not None}
    actual_accounts = {row.account_id: row.enabled for row in accounts if row.enabled is not None}
    expected_accounts = {scope.account_id: True} if scope.account_id is not None else {}
    return (
        item.default_enabled == scope.is_user
        and actual_tools == dict.fromkeys(scope.tools, True)
        and actual_accounts == expected_accounts
    )


def _initial_overrides(
    context: LibraryContext, item: SkillInstallation, scope: SkillScope, account: ToolAccount | None
) -> None:
    """
    仅首次安装展开显式范围，默认所有工具不创建当前工具列表副本。

    :param context (LibraryContext): 已锁定用户上下文
    :param item (SkillInstallation): 新安装记录
    :param scope (SkillScope): 首次请求范围
    :param account (ToolAccount | None): 已验证目标账户
    """
    for tool in scope.tools:
        context.repository.add(
            SkillToolOverride(
                user_id=context.user_id,
                installation_id=item.id,
                tool_type=tool,
                enabled=True,
                revision_id=None,
            )
        )
    if account is not None:
        context.repository.add(
            SkillAccountOverride(
                user_id=context.user_id,
                installation_id=item.id,
                account_id=account.id,
                tool_type=account.tool_type,
                enabled=True,
                revision_id=None,
            )
        )
