"""
提供有界安装包上传和仅凭已授权树引用的内容下载。
"""

import tempfile
from functools import partial
from typing import Annotated, BinaryIO, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.api.skill_streams import download_chunks, receive_verified_file
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_uploads import (
    SkillFileReceipt,
    SkillPackageUploadRequest,
    SkillTreeView,
    SkillUploadView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.storage.io import run_storage_io

router = APIRouter(prefix="/skills/content", tags=["skill-content"])
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


@router.post("/uploads")
async def begin_upload(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], request: Request
) -> SkillResult[SkillUploadView]:
    """
    先认证再有界读取完整清单，预留成功后返回可重试上传标识。

    :param context (SkillApiContext): 认证用户上下文
    :param request (Request): 原始 HTTP 流
    :return SkillResult[SkillUploadView]: 已持久化的上传计划
    """
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_MANIFEST_BYTES:
            raise SkillContentError("CONTENT_TOO_LARGE", "manifest body exceeds transport limit")
        raw.extend(chunk)
    try:
        payload = SkillPackageUploadRequest.model_validate_json(raw)
        upload = await context.content().begin(
            context.user.id, payload.idempotency_key, payload.manifest, "package"
        )
    except (ValidationError, ValueError) as error:
        if isinstance(error, SkillContentError):
            raise
        raise SkillContentError(
            "INVALID_REQUEST", "invalid package manifest or package limits"
        ) from error
    await context.session.commit()
    return _upload_result(upload)


@router.get("/uploads/{upload_id}")
async def upload_status(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], upload_id: UUID
) -> SkillResult[SkillUploadView]:
    """
    查询当前用户安装包租约，不暴露其他范围的上传。

    :param context (SkillApiContext): 认证用户上下文
    :param upload_id (UUID): 上传标识
    :return SkillResult[SkillUploadView]: 原计划和当前状态
    """
    upload = await _package_upload(context, upload_id)
    await context.session.commit()
    return _upload_result(upload)


@router.put("/uploads/{upload_id}/files/{digest}")
async def upload_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    request: Request,
    upload_id: UUID,
    digest: str,
) -> SkillResult[SkillFileReceipt]:
    """
    按已授权文件大小流式接收，任何超额或内容不符都不发布对象。

    :param context (SkillApiContext): 认证用户上下文
    :param request (Request): 原始二进制流
    :param upload_id (UUID): 持久化上传租约
    :param digest (str): 此计划内声明的文件摘要
    :return SkillResult[SkillFileReceipt]: 完整文件接收结果
    """
    await _package_upload(context, upload_id)
    service = context.content()
    entry = await service.prepare_file(context.user.id, upload_id, digest)
    async with receive_verified_file(request, entry) as source:
        created = await service.put_file(context.user.id, upload_id, digest, source)
        await context.session.commit()
    return SkillResult(
        status="persisted",
        committed=False,
        data=SkillFileReceipt(upload_id=upload_id, digest=digest, created=created),
    )


@router.post("/uploads/{upload_id}/complete")
async def complete_upload(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], upload_id: UUID
) -> SkillResult[SkillTreeView]:
    """
    完整校验所有文件后原子登记树，此时尚未安装到用户库。

    :param context (SkillApiContext): 认证用户上下文
    :param upload_id (UUID): 原上传租约
    :return SkillResult[SkillTreeView]: 可以被安装命令引用的完整包
    """
    await _package_upload(context, upload_id)
    try:
        tree = await context.content().complete(context.user.id, upload_id)
    except FileNotFoundError as error:
        raise SkillContentError(
            "CONTENT_INCOMPLETE", "some declared files have not been uploaded"
        ) from error
    except ValueError as error:
        if isinstance(error, SkillContentError):
            raise
        raise SkillContentError("CONTENT_INVALID", "stored file validation failed") from error
    await context.session.commit()
    return SkillResult(
        status="stored",
        committed=True,
        data=SkillTreeView(
            tree_digest=tree.digest, manifest=SkillTreeManifest.model_validate(tree.manifest_json)
        ),
    )


@router.get("/trees/{digest}")
async def read_tree(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], digest: str
) -> SkillResult[SkillTreeView]:
    """
    读取自己已经保存的完整清单，知道摘要不授予跨用户权限。

    :param context (SkillApiContext): 认证用户上下文
    :param digest (str): 树摘要
    :return SkillResult[SkillTreeView]: 完整安装包清单
    """
    manifest = await context.content().read_tree(context.user.id, "package", digest)
    await context.session.commit()
    return SkillResult(
        status="stored", committed=False, data=SkillTreeView(tree_digest=digest, manifest=manifest)
    )


@router.get("/trees/{tree_digest}/files/{file_digest}")
async def download_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    tree_digest: str,
    file_digest: str,
) -> StreamingResponse:
    """
    完整校验后发送有界文件流，不把尚未验证的损坏内容作为成功下载。

    :param context (SkillApiContext): 认证用户上下文
    :param tree_digest (str): 当前用户已提交树
    :param file_digest (str): 此树引用的文件
    :return StreamingResponse: 可验证的二进制流
    """
    # 暂存所有权移交给流式响应及断线清理，不能在路由返回前关闭。
    target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        await context.content().read_file(
            context.user.id, "package", tree_digest, file_digest, cast(BinaryIO, target)
        )
        size = await run_storage_io(target.tell)
        await run_storage_io(partial(target.seek, 0))
        await context.session.commit()
    except BaseException:
        await run_storage_io(target.close)
        raise
    return StreamingResponse(
        download_chunks(cast(BinaryIO, target)),
        media_type="application/octet-stream",
        headers={"Content-Length": str(size), "ETag": f'"{file_digest}"'},
        background=BackgroundTask(target.close),
    )


async def _package_upload(context: SkillApiContext, upload_id: UUID) -> SkillContentUpload:
    """
    用户包接口不能成为未来节点状态上传的旁路。

    :param context (SkillApiContext): 认证上下文
    :param upload_id (UUID): 请求上传标识
    :return SkillContentUpload: 当前用户安装包上传
    """
    upload = await context.content().get(context.user.id, upload_id)
    if upload.scope != "package":
        raise SkillContentError("UPLOAD_NOT_FOUND", "upload not found")
    return upload


def _upload_result(upload: SkillContentUpload) -> SkillResult[SkillUploadView]:
    """
    返回原始上传计划，不输出磁盘路径。

    :param upload (SkillContentUpload): 已授权包上传
    :return SkillResult[SkillUploadView]: 稳定状态封套
    """
    data = SkillUploadView.model_validate(
        {
            "id": upload.id,
            "status": upload.status,
            "tree_digest": upload.tree_digest,
            "reserved_bytes": upload.reserved_bytes,
            "expires_at": upload.expires_at,
            "manifest": upload.manifest_json,
        }
    )
    return SkillResult(status=upload.status, committed=upload.status == "committed", data=data)
