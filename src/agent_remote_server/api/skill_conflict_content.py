"""
提供用户冲突人工内容的有界上传，以及授权三侧完整内容导出。
"""

import hashlib
import tempfile
from functools import partial
from typing import Annotated, BinaryIO, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.api.skill_conflicts import conflict_service
from agent_remote_server.api.skill_streams import download_chunks, receive_verified_file
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_resolution import SkillResolutionUploadRequest
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_uploads import (
    SkillFileReceipt,
    SkillTreeView,
    SkillUploadView,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.storage.io import run_storage_io

router = APIRouter(prefix="/skills/state/conflicts/{publication_id}", tags=["skill-state-content"])
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


@router.post("/uploads")
async def begin_resolution_upload(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    request: Request,
) -> SkillResult[SkillUploadView]:
    """
    认证原始冲突后有界读取人工清单，上传本身不会保存解决计划。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始冲突范围
    :param request (Request): 网络清单流
    :return SkillResult[SkillUploadView]: 归属明确的原始上传计划
    """
    publication = await conflict_service(context).require(context.user.id, publication_id)
    if publication.status != "conflicted":
        raise SkillContentError(
            "CONFLICT_NOT_ACTIVE", "attempt cannot accept new resolution uploads"
        )
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_MANIFEST_BYTES:
            raise SkillContentError("CONTENT_TOO_LARGE", "manifest body exceeds transport limit")
        raw.extend(chunk)
    try:
        payload = SkillResolutionUploadRequest.model_validate_json(raw)
        key = _prefix(publication_id) + hashlib.sha256(payload.idempotency_key.encode()).hexdigest()
        upload = await context.content().begin(
            context.user.id, key, payload.manifest, "account_directory"
        )
    except (ValidationError, ValueError) as error:
        if isinstance(error, SkillContentError):
            raise
        raise SkillContentError("INVALID_REQUEST", "invalid resolution content manifest") from error
    await context.session.commit()
    return _upload_result(upload)


@router.get("/uploads/{upload_id}")
async def resolution_upload_status(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    upload_id: UUID,
) -> SkillResult[SkillUploadView]:
    """
    在同一用户和原冲突范围查询上传，不能替换成其他包或收尾上传。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始冲突
    :param upload_id (UUID): 上传尝试
    :return SkillResult[SkillUploadView]: 原始清单及当前租约状态
    """
    upload = await _require_upload(context, publication_id, upload_id)
    await context.session.commit()
    return _upload_result(upload)


@router.put("/uploads/{upload_id}/files/{digest}")
async def upload_resolution_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    upload_id: UUID,
    digest: str,
    request: Request,
) -> SkillResult[SkillFileReceipt]:
    """
    只接收已授权人工清单内的文件，流式校验大小与摘要。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原冲突范围
    :param upload_id (UUID): 原上传租约
    :param digest (str): 声明文件摘要
    :param request (Request): 实际字节流
    :return SkillResult[SkillFileReceipt]: 文件接收状态，不表示计划发布
    """
    await _require_upload(context, publication_id, upload_id)
    service = context.content()
    entry = await service.prepare_file(context.user.id, upload_id, digest)
    async with receive_verified_file(request, entry) as source:
        created = await service.put_file(context.user.id, upload_id, digest, source)
        await context.session.commit()
    return SkillResult(
        status="upload_pending",
        committed=False,
        data=SkillFileReceipt(upload_id=upload_id, digest=digest, created=created),
    )


@router.post("/uploads/{upload_id}/complete")
async def complete_resolution_upload(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    upload_id: UUID,
) -> SkillResult[SkillTreeView]:
    """
    所有字节齐备才保存人工树，解决选择仍需要独立 resolve 请求。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始冲突
    :param upload_id (UUID): 原上传尝试
    :return SkillResult[SkillTreeView]: 可供解决计划引用的完整私有状态树
    """
    await _require_upload(context, publication_id, upload_id)
    try:
        tree = await context.content().complete(context.user.id, upload_id)
    except FileNotFoundError as error:
        raise SkillContentError("CONTENT_INCOMPLETE", "resolution files are incomplete") from error
    except ValueError as error:
        if isinstance(error, SkillContentError):
            raise
        raise SkillContentError(
            "CONTENT_INVALID", "resolution content verification failed"
        ) from error
    await context.session.commit()
    return SkillResult(
        status="stored",
        committed=True,
        data=SkillTreeView(
            tree_digest=tree.digest, manifest=SkillTreeManifest.model_validate(tree.manifest_json)
        ),
    )


@router.get("/trees/{side}")
async def conflict_side_tree(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    side: Literal["base", "current", "incoming"],
) -> SkillResult[SkillTreeView]:
    """
    导出原始明确一侧完整清单，不能通过任意摘要选择其他账户树。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始尝试
    :param side (Literal["base", "current", "incoming"]): 明确原始侧
    :return SkillResult[SkillTreeView]: 完整保留清单
    """
    digest = await _side_digest(context, publication_id, side)
    tree = await context.content().read_tree(context.user.id, "state", digest)
    await context.session.commit()
    return SkillResult(
        status="stored", committed=False, data=SkillTreeView(tree_digest=digest, manifest=tree)
    )


@router.get("/trees/{side}/files/{digest}")
async def conflict_side_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    publication_id: UUID,
    side: Literal["base", "current", "incoming"],
    digest: str,
) -> StreamingResponse:
    """
    只导出该侧声明的已验证内容，发送前释放请求事务与用户锁。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始尝试
    :param side (Literal["base", "current", "incoming"]): 明确原始侧
    :param digest (str): 该树声明的文件摘要
    :return StreamingResponse: 有界且可验证的二进制流
    """
    tree_digest = await _side_digest(context, publication_id, side)
    target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        await context.content().read_file(
            context.user.id, "state", tree_digest, digest, cast(BinaryIO, target)
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
        headers={"Content-Length": str(size), "ETag": f'"{digest}"'},
        background=BackgroundTask(target.close),
    )


async def _side_digest(context: SkillApiContext, publication_id: UUID, side: str) -> str:
    """
    从授权原始引用取得摘要，绝不由请求提供账户或磁盘路径。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原始尝试
    :param side (str): 已验证侧名称
    :return str: 已保留的完整状态树摘要
    """
    info = await conflict_service(context).info(context.user.id, publication_id)
    selected = {"base": info.base, "current": info.current, "incoming": info.incoming}[side]
    if selected.tree_digest is None:
        raise SkillContentError("STATE_EXPIRED", "comparison tree is not retained")
    return selected.tree_digest


async def _require_upload(
    context: SkillApiContext, publication_id: UUID, upload_id: UUID
) -> SkillContentUpload:
    """
    同用户另一个冲突、原始收尾或安装上传都不能冒用当前人工上传入口。

    :param context (SkillApiContext): 用户授权上下文
    :param publication_id (UUID): 原冲突
    :param upload_id (UUID): 声明上传身份
    :return SkillContentUpload: 精确范围匹配的上传
    """
    await conflict_service(context).require(context.user.id, publication_id)
    upload = await context.content().get(context.user.id, upload_id)
    if upload.scope != "account_directory" or not upload.idempotency_key.startswith(
        _prefix(publication_id)
    ):
        raise SkillContentError("UPLOAD_NOT_FOUND", "resolution upload not found")
    return upload


def _prefix(publication_id: UUID) -> str:
    """
    上传键固定绑定冲突身份，客户端键只以摘要保存。

    :param publication_id (UUID): 授权冲突
    :return str: 无用户可替换字段的精确前缀
    """
    return f"resolve:{publication_id}:upload:"


def _upload_result(upload: SkillContentUpload) -> SkillResult[SkillUploadView]:
    """
    返回可恢复的清单和租约，不输出宿主路径。

    :param upload (SkillContentUpload): 已授权上传
    :return SkillResult[SkillUploadView]: 稳定上传状态
    """
    view = SkillUploadView.model_validate(
        {
            "id": upload.id,
            "status": upload.status,
            "tree_digest": upload.tree_digest,
            "reserved_bytes": upload.reserved_bytes,
            "expires_at": upload.expires_at,
            "manifest": upload.manifest_json,
        }
    )
    return SkillResult(status=upload.status, committed=upload.status == "committed", data=view)
