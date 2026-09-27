"""
只读分页重新构建同一截止的完整计划，最后一页才发出可执行确认。
"""

from itertools import islice
from uuid import UUID

from agent_remote_server.schemas.skill_prune import PrunePreviewPage, PrunePreviewRequest
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.prune import SkillPruneService
from agent_remote_server.services.skills.prune_commands.disclosure import disclosures, summary
from agent_remote_server.services.skills.prune_commands.tokens import PruneEnvelope, PruneTokens


async def preview_page(
    service: SkillPruneService, tokens: PruneTokens, user_id: UUID, request: PrunePreviewRequest
) -> PrunePreviewPage:
    """
    所有页复用原计划身份，不保存预览缓存、归属或任何回执。

    :param service (SkillPruneService): 已取得用户锁的内部只读服务
    :param tokens (PruneTokens): 当前部署的签名器
    :param user_id (UUID): 当前活跃所有者
    :param request (PrunePreviewRequest): 原范围或上页签名游标
    :return PrunePreviewPage: 完整连续页及下一阶段凭据
    """
    envelope = (
        tokens.verify(request.cursor, user_id, "page") if request.cursor is not None else None
    )
    if envelope is not None and (
        request.selector != envelope.binding.selector
        or request.all_unreferenced != envelope.binding.all_unreferenced
    ):
        raise SkillContentError("INVALID_REQUEST", "prune cursor scope does not match")
    plan = await service.preview(
        user_id,
        request.selector,
        all_unreferenced=request.all_unreferenced,
        cutoff=envelope.binding.cutoff if envelope is not None else None,
    )
    view = summary(plan)
    total = sum(1 for _ in disclosures(plan))
    if envelope is not None and (envelope.binding != view.binding or envelope.total != total):
        raise SkillContentError("HEAD_CHANGED", "prune preview changed; start a new preview")
    offset = envelope.offset if envelope is not None else 0
    rows = tuple(islice(disclosures(plan), offset, offset + request.limit))
    end = offset + len(rows)
    following = end < total
    credential = (
        tokens.sign(
            PruneEnvelope(
                user_id=user_id,
                binding=view.binding,
                offset=end,
                total=total,
                stage="page" if following else "confirm",
            )
        )
        if following or view.ready
        else None
    )
    return PrunePreviewPage(
        summary=view,
        offset=offset,
        total=total,
        rows=rows,
        next_cursor=credential if following else None,
        confirmation=None if following else credential,
    )
