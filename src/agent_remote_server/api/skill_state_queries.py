"""
提供用户检查点历史、待上传状态、明确基线差异和完整依赖导出。
"""

import tempfile
from functools import partial
from typing import Annotated, BinaryIO, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.api.skill_streams import download_chunks
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_state_queries import (
    SkillCheckpointDiff,
    SkillCheckpointMemberPage,
    SkillCheckpointPage,
    SkillCheckpointTree,
    SkillCheckpointView,
    SkillStatePendingPage,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.diagnostics import SkillDiagnosticService
from agent_remote_server.services.skills.state_diff import StateDiffService
from agent_remote_server.services.skills.state_queries import SkillStateQueryService
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state", tags=["skill-state"])


def query_service(context: SkillApiContext) -> SkillStateQueryService:
    """
    共享用户请求事务与独立内容策略。

    :param context (SkillApiContext): 用户认证上下文
    :return SkillStateQueryService: 授权状态查询服务
    """
    return SkillStateQueryService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.get("/checkpoints")
async def checkpoints(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    scope: Literal["item", "account-directory"] = "item",
    skill: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: UUID | None = None,
) -> SkillResult[SkillCheckpointPage]:
    """
    有界读取同一账户目录或稳定来源全部版本纪元的历史。

    :param context (SkillApiContext): 用户认证上下文
    :param account_id (UUID): 明确账户
    :param scope (Literal["item", "account-directory"]): 对象范围
    :param skill (str | None): 名称或稳定来源身份
    :param limit (int): 页大小
    :param cursor (UUID | None): 同范围检查点游标
    :return SkillResult[SkillCheckpointPage]: 有界历史页
    """
    result = await query_service(context).list(
        context.user.id, account_id, scope, skill, limit, cursor
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/pending")
async def pending(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    scope: Literal["item", "account-directory"] = "item",
    skill: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: UUID | None = None,
) -> SkillResult[SkillStatePendingPage]:
    """
    单独展示来源节点仍需上传的收尾，不能将其当作可恢复检查点。

    :param context (SkillApiContext): 用户认证上下文
    :param account_id (UUID): 明确账户
    :param scope (Literal["item", "account-directory"]): 对象范围
    :param skill (str | None): 名称或稳定来源身份
    :param limit (int): 页大小
    :param cursor (UUID | None): 收尾身份游标
    :return SkillResult[SkillStatePendingPage]: 尚待同步的元数据页
    """
    result = await query_service(context).pending(
        context.user.id, account_id, scope, skill, limit, cursor
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/checkpoints/{checkpoint_id}")
async def checkpoint_info(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    checkpoint_id: UUID,
) -> SkillResult[SkillCheckpointView]:
    """
    自动解释对象范围和保留位置，不要求用户猜测内部目录。

    :param context (SkillApiContext): 用户认证上下文
    :param checkpoint_id (UUID): 检查点身份
    :return SkillResult[SkillCheckpointView]: 精确分支及来源会话信息
    """
    service = query_service(context)
    result = await service.view(await service.require(context.user.id, checkpoint_id))
    diagnostics = SkillDiagnosticService(context.session, context.settings.skill_storage_policy)
    result.storage = await diagnostics.storage_view(context.user.id)
    result.retention = (
        await diagnostics.histories(context.user.id, "checkpoint", (checkpoint_id,))
    )[checkpoint_id]
    await context.session.commit()
    return SkillResult(
        status="ready" if result.retained else "state_expired", committed=False, data=result
    )


@router.get("/checkpoints/{checkpoint_id}/members")
async def checkpoint_members(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    checkpoint_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> SkillResult[SkillCheckpointMemberPage]:
    """
    读取完整目录中保留的原始成员身份和检查点引用。

    :param context (SkillApiContext): 用户认证上下文
    :param checkpoint_id (UUID): 完整目录检查点
    :param limit (int): 页大小
    :param cursor (str | None): 上页末尾成员名称
    :return SkillResult[SkillCheckpointMemberPage]: 有界历史成员引用
    """
    result = await query_service(context).members(context.user.id, checkpoint_id, limit, cursor)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/checkpoints/{checkpoint_id}/diff")
async def checkpoint_diff(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    checkpoint_id: UUID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
) -> SkillResult[SkillCheckpointDiff]:
    """
    对原始版本或目录父节点返回有界元数据差异，不输出正文。

    :param context (SkillApiContext): 用户认证上下文
    :param checkpoint_id (UUID): 检查点身份
    :param limit (int): 路径页大小
    :param cursor (str | None): 上页末尾路径
    :return SkillResult[SkillCheckpointDiff]: 明确基线的差异页
    """
    service = StateDiffService(query_service(context), SkillLocalRepository(context.session))
    result = await service.diff(context.user.id, checkpoint_id, limit, cursor)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/checkpoints/{checkpoint_id}/tree")
async def checkpoint_tree(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    checkpoint_id: UUID,
) -> SkillResult[SkillCheckpointTree]:
    """
    输出完整可验证导出单元，单项跨目录链接明确列出依赖范围。

    :param context (SkillApiContext): 用户认证上下文
    :param checkpoint_id (UUID): 检查点身份
    :return SkillResult[SkillCheckpointTree]: 保留原路径的导出清单
    """
    result = await query_service(context).tree(context.user.id, checkpoint_id)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/checkpoints/{checkpoint_id}/files/{digest}")
async def checkpoint_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    checkpoint_id: UUID,
    digest: str,
) -> StreamingResponse:
    """
    只读取本次导出单元声明的文件，校验后释放数据库事务再发送。

    :param context (SkillApiContext): 用户认证上下文
    :param checkpoint_id (UUID): 检查点身份
    :param digest (str): 导出单元声明的文件摘要
    :return StreamingResponse: 私有已验证文件流
    """
    service = query_service(context)
    tree = await service.tree(context.user.id, checkpoint_id)
    if not any(entry.kind == "file" and entry.sha256 == digest for entry in tree.manifest.entries):
        raise SkillContentError("CONTENT_NOT_FOUND", "file is not part of this export unit")
    target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        await service.content.read_file(
            context.user.id, "state", tree.source_tree_digest, digest, cast(BinaryIO, target)
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
