"""
提供迁移专用冲突查询和精确原始侧导出，用户身份不可替换。
"""

import tempfile
from functools import partial
from typing import Annotated, BinaryIO, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.api.skill_streams import download_chunks
from agent_remote_server.schemas.skill_migration_conflicts import (
    MigrationSide,
    SkillMigrationConflictDiff,
    SkillMigrationConflictPage,
    SkillMigrationConflictView,
    SkillMigrationTree,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.migration_conflict_content import (
    SkillMigrationConflictContent,
)
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state/migration/conflicts", tags=["skill-state"])


def migration_conflicts(context: SkillApiContext) -> SkillMigrationConflictService:
    """
    复用用户认证、功能开关及请求事务。

    :param context (SkillApiContext): 可信用户上下文
    :return SkillMigrationConflictService: 只读迁移服务
    """
    return SkillMigrationConflictService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


@router.get("")
async def list_migration_conflicts(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    skill: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: UUID | None = None,
) -> SkillResult[SkillMigrationConflictPage]:
    """
    查询账户或明确稳定来源下未解决和已失效迁移。

    :param context (SkillApiContext): 用户上下文
    :param account_id (UUID): 所属账户
    :param skill (str | None): 可选名称或稳定身份
    :param limit (int): 页大小
    :param cursor (UUID | None): 同范围末尾受理身份
    :return SkillResult[SkillMigrationConflictPage]: 无写入冲突页
    """
    result = await migration_conflicts(context).list(
        context.user.id, account_id, skill, limit, cursor
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/{migration_id}")
async def migration_conflict_info(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
) -> SkillResult[SkillMigrationConflictView]:
    """
    查询原始三侧、账户上下文和实时状态漂移。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 独立迁移受理身份
    :return SkillResult[SkillMigrationConflictView]: 原始记录与实时诊断
    """
    result = await migration_conflicts(context).info(context.user.id, migration_id)
    await context.session.commit()
    return SkillResult(status=result.status, committed=False, data=result)


@router.get("/{migration_id}/diff")
async def migration_conflict_diff(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=16384)] = None,
) -> SkillResult[SkillMigrationConflictDiff]:
    """
    元数据差异只解释保存输入，不代表批准后的迁移结果。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原始受理身份
    :param limit (int): 有界路径数量
    :param cursor (str | None): 同尝试及三侧摘要的游标
    :return SkillResult[SkillMigrationConflictDiff]: 原始三侧差异页
    """
    result = await SkillMigrationConflictContent(migration_conflicts(context)).diff(
        context.user.id, migration_id, limit, cursor
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/{migration_id}/trees/{side}")
async def migration_conflict_tree(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    side: MigrationSide,
) -> SkillResult[SkillMigrationTree]:
    """
    原样导出完整保存树，显式标注额外上下文根。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原始迁移身份
    :param side (MigrationSide): 明确保存侧
    :return SkillResult[SkillMigrationTree]: 未裁剪的完整清单
    """
    result = await SkillMigrationConflictContent(migration_conflicts(context)).tree(
        context.user.id, migration_id, side
    )
    await context.session.commit()
    return SkillResult(status="stored", committed=False, data=result)


@router.get("/{migration_id}/trees/{side}/files/{digest}")
async def migration_conflict_file(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    migration_id: UUID,
    side: MigrationSide,
    digest: str,
) -> StreamingResponse:
    """
    发送前完整验证该侧声明文件，网络流不持有数据库锁。

    :param context (SkillApiContext): 用户上下文
    :param migration_id (UUID): 原始迁移身份
    :param side (MigrationSide): 已保存原始侧
    :param digest (str): 该侧声明文件摘要
    :return StreamingResponse: 已完整验证的二进制文件
    """
    result = await SkillMigrationConflictContent(migration_conflicts(context)).tree(
        context.user.id, migration_id, side
    )
    assert result.input.tree_digest is not None
    target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        await context.content().read_file(
            context.user.id, "state", result.input.tree_digest, digest, cast(BinaryIO, target)
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
