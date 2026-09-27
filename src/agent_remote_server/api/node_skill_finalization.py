"""
提供精确快照收尾计划、有界文件上传与完整持久化确认。
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_node, get_session, get_settings
from agent_remote_server.api.skill_streams import receive_verified_file
from agent_remote_server.config import Settings
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_finalizations import (
    SkillFinalizationRequest,
    SkillFinalizationView,
)
from agent_remote_server.schemas.skill_publications import SkillPublicationView
from agent_remote_server.schemas.skill_reclamation import SkillReclamationAuthorization
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_uploads import SkillFileReceipt
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization import SkillFinalizationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/node", tags=["node-skill-finalization"])
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


@dataclass
class NodeFinalizationContext:
    """
    节点身份与事务由认证层提供，请求不能覆盖。
    """

    node: Node
    session: AsyncSession
    service: SkillFinalizationService


def get_finalization_context(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NodeFinalizationContext:
    """
    先完成节点认证和功能开关检查，再允许读取上传正文。

    :param node (Node): 认证节点
    :param session (AsyncSession): 请求事务
    :param settings (Settings): 部署配置
    :return NodeFinalizationContext: 已认证上下文
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    return NodeFinalizationContext(
        node,
        session,
        SkillFinalizationService(
            session, PrivateObjectStore(settings.skill_storage_root), settings.skill_storage_policy
        ),
    )


@router.post("/skill-snapshots/{snapshot_id}/finalization")
async def begin_finalization(
    context: Annotated[NodeFinalizationContext, Depends(get_finalization_context)],
    snapshot_id: UUID,
    request: Request,
) -> SkillResult[SkillFinalizationView]:
    """
    有界读取完整清单，幂等受理或续期同一不可变收尾输入。

    :param context (NodeFinalizationContext): 认证节点上下文
    :param snapshot_id (UUID): Server 精确快照
    :param request (Request): 原始网络请求
    :return SkillResult[SkillFinalizationView]: 当前持久化回执
    """
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_MANIFEST_BYTES:
            raise SkillContentError("CONTENT_TOO_LARGE", "manifest body exceeds transport limit")
        raw.extend(chunk)
    try:
        payload = SkillFinalizationRequest.model_validate_json(raw)
    except ValidationError as error:
        raise SkillContentError("INVALID_REQUEST", "invalid finalization manifest") from error
    result = await context.service.begin(context.node.id, snapshot_id, payload)
    await context.session.commit()
    return _result(result)


@router.get("/skill-finalizations/{finalization_id}")
async def finalization_status(
    context: Annotated[NodeFinalizationContext, Depends(get_finalization_context)],
    finalization_id: UUID,
) -> SkillResult[SkillFinalizationView]:
    """
    查询当前尝试和内容持久化状态，不自动延长过期租约。

    :param context (NodeFinalizationContext): 认证节点上下文
    :param finalization_id (UUID): 原始收尾标识
    :return SkillResult[SkillFinalizationView]: 稳定状态回执
    """
    result = await context.service.get(context.node.id, finalization_id)
    await context.session.commit()
    return _result(result)


@router.put("/skill-finalizations/{finalization_id}/files/{digest}")
async def upload_finalization_file(
    context: Annotated[NodeFinalizationContext, Depends(get_finalization_context)],
    finalization_id: UUID,
    digest: str,
    upload_id: UUID,
    request: Request,
) -> SkillResult[SkillFileReceipt]:
    """
    当前尝试仅能接收原始完整清单声明的文件，大小和摘要双重验证。

    :param context (NodeFinalizationContext): 认证节点上下文
    :param finalization_id (UUID): 原始收尾标识
    :param digest (str): 声明的文件摘要
    :param upload_id (UUID): 当前上传尝试
    :param request (Request): 原始字节流
    :return SkillResult[SkillFileReceipt]: 单文件接收状态
    """
    entry = await context.service.prepare_file(context.node.id, finalization_id, upload_id, digest)
    async with receive_verified_file(request, entry) as source:
        created = await context.service.put_file(
            context.node.id, finalization_id, upload_id, digest, source
        )
        await context.session.commit()
    return SkillResult(
        status="upload_pending",
        committed=False,
        data=SkillFileReceipt(upload_id=upload_id, digest=digest, created=created),
    )


@router.get("/skill-finalizations/{finalization_id}/reclamation-authorization")
async def finalization_reclamation_authorization(
    context: Annotated[NodeFinalizationContext, Depends(get_finalization_context)],
    response: Response,
    finalization_id: UUID,
    request_id: UUID,
) -> SkillResult[SkillReclamationAuthorization]:
    """
    原节点必须取得完整内容核验，不能凭历史回执删除本地唯一副本。

    :param context (NodeFinalizationContext): 认证节点和同一请求事务
    :param response (Response): 禁止缓存授权的响应上下文
    :param finalization_id (UUID): 原始收尾标识
    :param request_id (UUID): 当前节点生成的随机挑战
    :return SkillResult[SkillReclamationAuthorization]: 已验证远端保存的短期核验封套
    """
    result = await context.service.authorize_reclamation(
        context.node.id, finalization_id, request_id
    )
    await context.session.commit()
    response.headers["Cache-Control"] = "no-store"
    return SkillResult(status="reclaimable", committed=True, data=result)


@router.post("/skill-finalizations/{finalization_id}/complete")
async def complete_finalization(
    context: Annotated[NodeFinalizationContext, Depends(get_finalization_context)],
    finalization_id: UUID,
    upload_id: UUID,
) -> SkillResult[SkillFinalizationView]:
    """
    完整输入校验和引用提交后确认 persisted，不提前声明账户发布成功。

    :param context (NodeFinalizationContext): 认证节点上下文
    :param finalization_id (UUID): 原始收尾标识
    :param upload_id (UUID): 当前上传尝试
    :return SkillResult[SkillFinalizationView]: 完整内容持久化回执
    """
    result = await context.service.complete(context.node.id, finalization_id, upload_id)
    await context.session.commit()
    return _result(result)


@router.post("/skill-finalizations/{finalization_id}/publish")
async def publish_finalization(
    context: Annotated[NodeFinalizationContext, Depends(get_finalization_context)],
    finalization_id: UUID,
) -> SkillResult[SkillPublicationView]:
    """
    只对原节点已完整保存的输入执行目录原子发布或冲突归档。

    :param context (NodeFinalizationContext): 已认证节点上下文
    :param finalization_id (UUID): 原始收尾身份
    :return SkillResult[SkillPublicationView]: 已提交的完整发布回执
    """
    result = await context.service.publish(context.node.id, finalization_id)
    await context.session.commit()
    return SkillResult(status=result.status, committed=True, data=result)


def _result(view: SkillFinalizationView) -> SkillResult[SkillFinalizationView]:
    """
    仅完整 checkpoint 已保留时确认内容提交。

    :param view (SkillFinalizationView): 当前收尾回执
    :return SkillResult[SkillFinalizationView]: 区分上传与发布的状态封套
    """
    return SkillResult(status=view.status, committed=view.status != "upload_pending", data=view)
