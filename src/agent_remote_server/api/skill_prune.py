"""
开放已认证 prune 预览、完整确认和独立回执查询，物理删除进度不改写受理结果。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_prune import (
    PruneCommand,
    PruneDeletionProgress,
    PrunePreviewPage,
    PrunePreviewRequest,
    PruneReceipt,
    PruneReceiptPage,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.prune_commands import SkillPruneCommandService
from agent_remote_server.services.skills.prune_commands.queries import PruneQueries
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state/prune", tags=["skill-state"])


def command_service(context: SkillApiContext) -> SkillPruneCommandService:
    """
    仅构造延迟访问内容的同事务依赖。

    :param context (SkillApiContext): 活跃用户认证上下文
    :return SkillPruneCommandService: 当前部署配置的命令服务
    """
    return SkillPruneCommandService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
        context.settings.secret_key,
    )


@router.post("/preview")
async def preview(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    payload: PrunePreviewRequest,
) -> SkillResult[PrunePreviewPage]:
    """
    只读分页完整披露损失，全部页遍历结束才提供最终确认凭据。

    :param context (SkillApiContext): 活跃用户上下文
    :param payload (PrunePreviewRequest): 明确范围及可选原分页游标
    :return SkillResult[PrunePreviewPage]: 连续完整披露页
    """
    result = await command_service(context).preview(context.user.id, payload)
    await context.session.commit()
    return SkillResult(status="preview", committed=False, data=result)


@router.post("")
async def execute(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    payload: PruneCommand,
) -> SkillResult[PruneReceipt]:
    """
    原子保存清理和回执后才返回受理，不承诺文件已从磁盘删除。

    :param context (SkillApiContext): 活跃用户上下文
    :param payload (PruneCommand): 原始命令键与最终确认凭据
    :return SkillResult[PruneReceipt]: 已提交不可变受理
    """
    result = await command_service(context).execute(context.user.id, payload)
    await context.session.commit()
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )


@router.get("/operations")
async def by_key(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: Annotated[str, Query(min_length=1, max_length=128)],
) -> SkillResult[PruneReceipt]:
    """
    断线恢复先查询原键，不要求旧内容或旧签名密钥仍然存在。

    :param context (SkillApiContext): 活跃用户上下文
    :param key (str): 原始请求键
    :return SkillResult[PruneReceipt]: 原始已受理结果
    """
    result = await PruneQueries(context.session).by_key(context.user.id, key)
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )


@router.get("/operations/{operation_id}")
async def by_id(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
) -> SkillResult[PruneReceipt]:
    """
    按稳定操作身份读取同所有者回执。

    :param context (SkillApiContext): 活跃用户上下文
    :param operation_id (UUID): 原受理身份
    :return SkillResult[PruneReceipt]: 原始已受理结果
    """
    result = await PruneQueries(context.session).by_id(context.user.id, operation_id)
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )


@router.get("/operations/{operation_id}/entries")
async def entries(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> SkillResult[PruneReceiptPage]:
    """
    有界恢复全部原披露和实际替换身份，不访问已回收输入。

    :param context (SkillApiContext): 活跃用户上下文
    :param operation_id (UUID): 原受理身份
    :param offset (int): 首条原序号
    :param limit (int): 单页条数上限
    :return SkillResult[PruneReceiptPage]: 原持久化详情页
    """
    result = await PruneQueries(context.session).entries(
        context.user.id, operation_id, offset, limit
    )
    return SkillResult(status="accepted", committed=True, operation_id=operation_id, data=result)


@router.get("/operations/{operation_id}/progress")
async def progress(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
) -> SkillResult[PruneDeletionProgress]:
    """
    当前物理进度只汇总原操作实际关联的持久化任务。

    :param context (SkillApiContext): 活跃用户上下文
    :param operation_id (UUID): 原受理身份
    :return SkillResult[PruneDeletionProgress]: 独立物理进度
    """
    result = await PruneQueries(context.session).progress(context.user.id, operation_id)
    return SkillResult(status="accepted", committed=True, operation_id=operation_id, data=result)
