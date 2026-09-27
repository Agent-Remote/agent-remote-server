"""
提供精确任务授权的首次接管传输，不提供预约或自动派发入口。
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_node, get_session, get_settings
from agent_remote_server.api.skill_streams import receive_verified_file
from agent_remote_server.config import Settings
from agent_remote_server.models import Node
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_takeover import (
    SkillTakeoverCapture,
    SkillTakeoverLease,
    SkillTakeoverLeaseRequest,
    SkillTakeoverView,
    SkillTakeoverWriter,
)
from agent_remote_server.schemas.skill_uploads import SkillFileReceipt
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.takeover import SkillAccountTakeoverService

router = APIRouter(prefix="/node/skill-takeovers", tags=["node-skill-takeover"])
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024


@dataclass
class NodeTakeoverContext:
    """
    身份与事务由现有节点认证依赖提供。
    """

    node: Node
    session: AsyncSession
    service: SkillAccountTakeoverService


def get_takeover_context(
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> NodeTakeoverContext:
    """
    功能开关与节点认证先于正文读取。

    :param node (Node): 认证节点
    :param session (AsyncSession): 请求事务
    :param settings (Settings): 部署配置
    :return NodeTakeoverContext: 已认证的服务上下文
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    return NodeTakeoverContext(node, session, SkillAccountTakeoverService(session, settings))


@router.get("/{takeover_id}")
async def takeover_status(
    context: Annotated[NodeTakeoverContext, Depends(get_takeover_context)],
    takeover_id: UUID,
    task_id: UUID,
) -> SkillResult[SkillTakeoverView]:
    """
    返回不可变写入者清单，允许先关闭围栏再等待旧写入者自然退出。

    :param context (NodeTakeoverContext): 认证节点上下文
    :param takeover_id (UUID): 原始预约身份
    :param task_id (UUID): 精确任务身份
    :return SkillResult[SkillTakeoverView]: 当前授权和持久化状态
    """
    receipt = await context.service.get(context.node.id, takeover_id, task_id)
    result = _result(receipt)
    await context.session.commit()
    return result


@router.post("/{takeover_id}/capture")
async def begin_takeover_capture(
    context: Annotated[NodeTakeoverContext, Depends(get_takeover_context)],
    takeover_id: UUID,
    task_id: UUID,
    request: Request,
) -> SkillResult[SkillTakeoverView]:
    """
    精确任务先授权再有界读取，持久化原始捕获或续期相同输入。

    :param context (NodeTakeoverContext): 认证节点上下文
    :param takeover_id (UUID): 原始预约身份
    :param task_id (UUID): 精确任务身份
    :param request (Request): 完整捕获声明
    :return SkillResult[SkillTakeoverView]: 当前上传尝试或原始提交收据
    """
    await context.service.get(context.node.id, takeover_id, task_id)
    await context.session.commit()
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_CAPTURE_BYTES:
            raise SkillContentError("CONTENT_TOO_LARGE", "capture body exceeds transport limit")
        raw.extend(chunk)
    try:
        payload = SkillTakeoverCapture.model_validate_json(raw)
    except ValidationError as error:
        raise SkillContentError("INVALID_REQUEST", "invalid takeover capture") from error
    receipt = await context.service.begin_capture(context.node.id, takeover_id, task_id, payload)
    result = _result(receipt)
    await context.session.commit()
    return result


@router.put("/{takeover_id}/files/{digest}")
async def upload_takeover_file(
    context: Annotated[NodeTakeoverContext, Depends(get_takeover_context)],
    takeover_id: UUID,
    task_id: UUID,
    upload_id: UUID,
    digest: str,
    request: Request,
) -> SkillResult[SkillFileReceipt]:
    """
    只接收当前上传清单声明的真实字节，落库前再次核对租约。

    :param context (NodeTakeoverContext): 认证节点上下文
    :param takeover_id (UUID): 原始预约身份
    :param task_id (UUID): 精确任务身份
    :param upload_id (UUID): 当前上传尝试
    :param digest (str): 清单文件摘要
    :param request (Request): 有界原始字节流
    :return SkillResult[SkillFileReceipt]: 单文件持久化状态
    """
    entry = await context.service.prepare_file(
        context.node.id, takeover_id, task_id, upload_id, digest
    )
    await context.session.commit()
    async with receive_verified_file(request, entry) as source:
        created = await context.service.put_file(
            context.node.id, takeover_id, task_id, upload_id, digest, source
        )
        await context.session.commit()
    return SkillResult(
        status="upload_pending",
        committed=False,
        data=SkillFileReceipt(upload_id=upload_id, digest=digest, created=created),
    )


@router.post("/{takeover_id}/lease")
async def renew_takeover_lease(
    context: Annotated[NodeTakeoverContext, Depends(get_takeover_context)],
    takeover_id: UUID,
    task_id: UUID,
    payload: SkillTakeoverLeaseRequest,
) -> SkillResult[SkillTakeoverLease]:
    """
    独立短事务续期本次领取，网络上传不能阻塞该事务。

    :param context (NodeTakeoverContext): 认证上下文
    :param takeover_id (UUID): 原始接管身份
    :param task_id (UUID): 精确任务身份
    :param payload (SkillTakeoverLeaseRequest): 本次领取序号
    :return SkillResult[SkillTakeoverLease]: 未改变目录权威的活动租约
    """
    result = await context.service.renew_lease(context.node.id, takeover_id, task_id, payload)
    await context.session.commit()
    return SkillResult(status="leased", committed=False, data=result)


@router.post("/{takeover_id}/complete")
async def complete_takeover(
    context: Annotated[NodeTakeoverContext, Depends(get_takeover_context)],
    takeover_id: UUID,
    task_id: UUID,
    upload_id: UUID,
) -> SkillResult[SkillTakeoverView]:
    """
    全部内容、账户本地来源和目录权威提交后才返回已提交回执。

    :param context (NodeTakeoverContext): 认证节点上下文
    :param takeover_id (UUID): 原始预约身份
    :param task_id (UUID): 精确任务身份
    :param upload_id (UUID): 当前完整上传尝试
    :return SkillResult[SkillTakeoverView]: 原始权威提交收据
    """
    try:
        receipt = await context.service.complete(context.node.id, takeover_id, task_id, upload_id)
    except FileNotFoundError as error:
        raise SkillContentError("CONTENT_INCOMPLETE", "takeover files are incomplete") from error
    except ValueError as error:
        if isinstance(error, SkillContentError):
            raise
        raise SkillContentError("CONTENT_INVALID", "takeover content validation failed") from error
    result = _result(receipt)
    await context.session.commit()
    return result


def _result(receipt: SkillAccountTakeover) -> SkillResult[SkillTakeoverView]:
    """
    显式选择允许返回的身份，原始文件与用户幂等键不能进入响应。

    :param receipt (SkillAccountTakeover): 已授权持久化记录
    :return SkillResult[SkillTakeoverView]: 不含原始内容的状态封套
    """
    view = SkillTakeoverView.model_validate(
        {
            "takeover_id": receipt.id,
            "task_id": receipt.task_id,
            "node_id": receipt.node_id,
            "user_id": receipt.user_id,
            "account_id": receipt.account_id,
            "runtime_backend": receipt.runtime_backend,
            "directory_epoch": receipt.directory_epoch,
            "inventory_digest": receipt.inventory_digest,
            "inventory": [
                SkillTakeoverWriter.model_validate(item) for item in receipt.inventory_json
            ],
            "status": receipt.status,
            "helper_receipt_id": receipt.helper_receipt_id,
            "capture_digest": receipt.capture_digest,
            "upload_id": receipt.upload_id,
            "upload_attempt": receipt.upload_attempt,
            "checkpoint_id": receipt.checkpoint_id,
        }
    )
    return SkillResult(status=view.status, committed=view.status == "committed", data=view)
