"""
提供有界且永久只读的人工清单预览，文件内容仍须独立上传并校验。
"""

from functools import partial
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.api.skill_conflicts import conflict_service
from agent_remote_server.schemas.skill_resolution_preview import (
    SkillResolutionContentPreview,
    SkillResolutionPreviewRequest,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.resolution_preview import SkillResolutionPreviewService
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state", tags=["skill-state"])
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


@router.post("/conflicts/{publication_id}/content-preview")
async def publication_content_preview(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    request: Request,
) -> SkillResult[SkillResolutionContentPreview]:
    """
    用户授权后读取清单，只计算原会话冲突的拟议元数据变化。

    :param context (SkillApiContext): 用户请求上下文
    :param publication_id (UUID): 原冲突身份
    :param request (Request): 有界清单网络流
    :return SkillResult[SkillResolutionContentPreview]: 无操作身份且未提交的预览
    """
    return await _preview(context, publication_id, request, "publication")


@router.post("/migration/conflicts/{migration_id}/content-preview")
async def migration_content_preview(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    request: Request,
) -> SkillResult[SkillResolutionContentPreview]:
    """
    按原迁移固定输入预览人工清单，不授予内容引用或来源写入权限。

    :param context (SkillApiContext): 用户请求上下文
    :param migration_id (UUID): 原迁移身份
    :param request (Request): 有界清单网络流
    :return SkillResult[SkillResolutionContentPreview]: 元数据候选与待执行检查
    """
    return await _preview(context, migration_id, request, "migration")


async def _preview(
    context: SkillApiContext,
    conflict_id: UUID,
    request: Request,
    kind: Literal["publication", "migration"],
) -> SkillResult[SkillResolutionContentPreview]:
    """
    接收网络流前释放读取事务，清单完成后重新授权并核对原计划版本。

    :param context (SkillApiContext): 用户请求上下文
    :param conflict_id (UUID): 精确原尝试
    :param request (Request): 网络元数据流
    :param kind (Literal["publication", "migration"]): 原尝试身份域
    :return SkillResult[SkillResolutionContentPreview]: 永不提交变更的预览结果
    """
    service = SkillResolutionPreviewService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )
    if kind == "publication":
        await conflict_service(context).require(context.user.id, conflict_id)
    else:
        await service.migrations.conflicts.require(context.user.id, conflict_id)
    await context.session.commit()
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_MANIFEST_BYTES:
            raise SkillContentError(
                "CONTENT_TOO_LARGE", "preview manifest body exceeds transport limit"
            )
        raw.extend(chunk)
    try:
        payload = await run_storage_io(
            partial(SkillResolutionPreviewRequest.model_validate_json, raw)
        )
    except (ValidationError, ValueError) as error:
        raise SkillContentError("INVALID_REQUEST", "invalid preview manifest or choice") from error
    result = await (
        service.publication(context.user.id, conflict_id, payload)
        if kind == "publication"
        else service.migration(context.user.id, conflict_id, payload)
    )
    await context.session.commit()
    return SkillResult(status="preview", committed=False, data=result)
