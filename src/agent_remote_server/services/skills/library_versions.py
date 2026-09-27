"""
管理候选版本、默认激活历史、上游策略和逻辑卸载。
"""

from datetime import UTC, datetime

from agent_remote_server.schemas.skill_library import (
    SkillRemoveRequest,
    SkillRollbackRequest,
    SkillSource,
    SkillUpdateRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import LibraryChange, LibraryContext


async def update_skill(context: LibraryContext, request: SkillUpdateRequest) -> LibraryChange:
    """
    登记候选或激活相同来源版本，原版本 provenance 永不重写。

    :param context (LibraryContext): 已锁定用户库上下文
    :param request (SkillUpdateRequest): 单项更新计划
    :return LibraryChange: 登记和激活结果
    """
    item = await context.require_skill(request.skill)
    candidate = request.item
    source = SkillSource.model_validate(item.source_json)
    if candidate.name != item.name or candidate.source.subpath != source.subpath:
        raise SkillContentError(
            "SOURCE_LAYOUT_CHANGED",
            "source name or subpath changed",
            details={
                "expected": {"name": item.name, "subpath": source.subpath},
                "actual": {"name": candidate.name, "subpath": candidate.source.subpath},
            },
        )
    if candidate.source.identity() != item.source_key:
        raise SkillContentError("SKILL_SOURCE_CONFLICT", "update cannot replace a skill's source")
    await context.validate_observation(item, candidate)
    if source.kind == "git" and not request.switch_tracking:
        tracking = item.tracking_json
        if (
            tracking.get("ref_kind") != "branch"
            or candidate.provenance.ref_kind != "branch"
            or tracking.get("ref") != candidate.provenance.ref
        ):
            raise SkillContentError("SOURCE_PINNED", "switch a pinned source with an explicit ref")
    metadata = await context.validate_package(candidate)
    revision, created = await context.register_revision(item, candidate, metadata)
    change = LibraryChange(
        skills=[item], revisions=[revision], changed=created, deploy=not request.stage
    )
    if not request.stage:
        activated = context.activate(item, revision)
        tracking = candidate.provenance.model_dump(mode="json")
        change.changed = change.changed or activated or item.tracking_json != tracking
        item.tracking_json = tracking
    return change


async def rollback_skill(context: LibraryContext, request: SkillRollbackRequest) -> LibraryChange:
    """
    回滚只采用实际激活历史，不会误选只登记过的候选版本。

    :param context (LibraryContext): 已锁定用户上下文
    :param request (SkillRollbackRequest): 回滚选择
    :return LibraryChange: 默认版本和上游策略的变更
    """
    item = await context.require_skill(request.skill)
    if request.revision is None:
        revision = await context.repository.previous_activation(item)
        if revision is None:
            raise SkillContentError(
                "NO_ROLLBACK_HISTORY", "no previous different activated revision"
            )
        revision = await context.require_revision(item, str(revision.id))
    else:
        revision = await context.require_revision(item, request.revision)
    tracking: dict[str, object] = {"ref_kind": "fixed", "revision_id": str(revision.id)}
    activated = context.activate(item, revision)
    changed = activated or item.tracking_json != tracking
    item.tracking_json = tracking
    return LibraryChange(skills=[item], revisions=[revision], changed=changed)


async def remove_skill(context: LibraryContext, request: SkillRemoveRequest) -> LibraryChange:
    """
    归档当前安装 epoch，保留版本、覆盖和后续旧会话收尾所需身份。

    :param context (LibraryContext): 已锁定用户上下文
    :param request (SkillRemoveRequest): 单项卸载请求
    :return LibraryChange: 已归档安装及受影响范围
    """
    item = await context.require_skill(request.skill, active=False)
    if item.removed:
        return LibraryChange(skills=[item])
    epoch = await context.repository.epoch(item)
    epoch.archived_at = datetime.now(UTC)
    item.removed = True
    return LibraryChange(
        skills=[item],
        changed=True,
        warnings=["retained revisions, account overrides and runtime checkpoints"],
    )
