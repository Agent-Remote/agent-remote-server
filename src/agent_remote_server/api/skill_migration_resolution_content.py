"""
提供迁移专用人工上传和只读计划查询，不将上传完成误报为迁移发布。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.api.skill_streams import receive_verified_file
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_migration_resolution import SkillMigrationResolutionPlanView
from agent_remote_server.schemas.skill_resolution import SkillResolutionUploadRequest
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_uploads import (
    SkillFileReceipt,
    SkillTreeView,
    SkillUploadView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(
    prefix="/skills/state/migration/conflicts/{migration_id}", tags=["skill-state-content"]
)
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


def migration_resolution_content(
    context: SkillApiContext,
) -> SkillMigrationResolutionContentService:
    """
    使用相同用户认证、功能开关和请求事务。

    :param context (SkillApiContext): 已认证上下文
    :return SkillMigrationResolutionContentService: 迁移人工内容服务
    """
    return SkillMigrationResolutionContentService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.get("/plan")
async def migration_resolution_plan(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
) -> SkillResult[SkillMigrationResolutionPlanView]:
    """
    返回原始迁移计划版本，读取不会建立空计划。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原迁移身份
    :return SkillResult[SkillMigrationResolutionPlanView]: 只读计划状态
    """
    result = await migration_resolution_content(context).plan(context.user.id, migration_id)
    await context.session.commit()
    return SkillResult(status=result.current_status, committed=False, data=result)


@router.post("/uploads")
async def begin_migration_resolution_upload(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    request: Request,
) -> SkillResult[SkillUploadView]:
    """
    先授权原冲突，再有界接收清单并恢复原租约或原子保存新绑定。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原迁移身份
    :param request (Request): 完整清单网络流
    :return SkillResult[SkillUploadView]: 独立人工上传受理结果
    """
    service = migration_resolution_content(context)
    await service.conflicts.require(context.user.id, migration_id)
    await context.session.commit()
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_MANIFEST_BYTES:
            raise SkillContentError("CONTENT_TOO_LARGE", "manifest body exceeds transport limit")
        raw.extend(chunk)
    try:
        payload = SkillResolutionUploadRequest.model_validate_json(raw)
    except ValidationError as error:
        raise SkillContentError(
            "INVALID_REQUEST", "invalid migration resolution manifest"
        ) from error
    upload = await service.begin(context.user.id, migration_id, payload)
    await context.session.commit()
    return upload_result(upload)


@router.get("/uploads/{upload_id}")
async def migration_resolution_upload_status(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    upload_id: UUID,
) -> SkillResult[SkillUploadView]:
    """
    真实迁移绑定授权上传，键前缀或相同摘要不能替用。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原迁移身份
    :param upload_id (UUID): 原上传租约
    :return SkillResult[SkillUploadView]: 可恢复的租约状态
    """
    upload = await migration_resolution_content(context).get(
        context.user.id, migration_id, upload_id
    )
    await context.session.commit()
    return upload_result(upload)


@router.put("/uploads/{upload_id}/files/{digest}")
async def migration_resolution_upload_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    upload_id: UUID,
    digest: str,
    request: Request,
) -> SkillResult[SkillFileReceipt]:
    """
    对精确租约声明文件有界接收并验证内容，不隐式保存解决选择。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原迁移身份
    :param upload_id (UUID): 原租约
    :param digest (str): 声明摘要
    :param request (Request): 文件网络流
    :return SkillResult[SkillFileReceipt]: 实际文件接收回执
    """
    service = migration_resolution_content(context)
    entry = await service.prepare_file(context.user.id, migration_id, upload_id, digest)
    async with receive_verified_file(request, entry) as source:
        created = await service.put_file(context.user.id, migration_id, upload_id, digest, source)
        await context.session.commit()
    return SkillResult(
        status="upload_pending",
        committed=False,
        data=SkillFileReceipt(
            upload_id=upload_id,
            digest=digest,
            created=created,
        ),
    )


@router.post("/uploads/{upload_id}/complete")
async def complete_migration_resolution_upload(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    upload_id: UUID,
) -> SkillResult[SkillTreeView]:
    """
    完整状态树及迁移授权原子保存，不改变计划、冲突状态或成功基线。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原迁移身份
    :param upload_id (UUID): 原租约
    :return SkillResult[SkillTreeView]: 完整已保活的人工树
    """
    tree = await migration_resolution_content(context).complete(
        context.user.id, migration_id, upload_id
    )
    await context.session.commit()
    return SkillResult(
        status="stored",
        committed=True,
        data=SkillTreeView(
            tree_digest=tree.digest,
            manifest=SkillTreeManifest.model_validate(tree.manifest_json),
        ),
    )


def upload_result(upload: SkillContentUpload) -> SkillResult[SkillUploadView]:
    """
    受理状态只描述上传，不能暗示迁移已经发布。

    :param upload (SkillContentUpload): 已授权租约
    :return SkillResult[SkillUploadView]: 完整上传视图
    """
    return SkillResult(
        status=upload.status,
        committed=upload.status == "committed",
        data=SkillUploadView.model_validate(
            {
                "id": upload.id,
                "status": upload.status,
                "tree_digest": upload.tree_digest,
                "reserved_bytes": upload.reserved_bytes,
                "expires_at": upload.expires_at,
                "manifest": upload.manifest_json,
            }
        ),
    )
