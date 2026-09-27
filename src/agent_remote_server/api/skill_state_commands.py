"""
提供当前有效状态查询、明确重置恢复和断线幂等结果查询。
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_state_commands import (
    SkillCurrentStateView,
    SkillStateCommand,
    SkillStateCommandView,
    SkillStateSelector,
)
from agent_remote_server.schemas.skill_state_queries import SkillCheckpointDiff
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_commands import SkillStateCommandService
from agent_remote_server.services.skills.state_diff import StateDiffService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

router = APIRouter(prefix="/skills/state", tags=["skill-state"])


def command_service(context: SkillApiContext) -> SkillStateCommandService:
    """
    用认证上下文构造同事务状态服务。

    :param context (SkillApiContext): 活跃用户认证上下文
    :return SkillStateCommandService: 同事务服务
    """
    return SkillStateCommandService(
        context.session,
        PrivateObjectStore(context.settings.skill_storage_root),
        context.settings.skill_storage_policy,
    )


def selector(
    account_id: UUID, scope: Literal["item", "account-directory"], skill: str | None
) -> SkillStateSelector:
    """
    HTTP 查询参数也必须满足单项与目录互斥约束。

    :param account_id (UUID): 指定账户
    :param scope (Literal["item", "account-directory"]): 对象范围
    :param skill (str | None): 单项来源
    :return SkillStateSelector: 严格范围
    """
    try:
        return SkillStateSelector(account_id=account_id, scope=scope, skill=skill)
    except ValidationError as error:
        raise SkillContentError(
            "INVALID_REQUEST", "select one skill or account-directory scope"
        ) from error


@router.get("/current")
async def current_state(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    scope: Literal["item", "account-directory"] = "item",
    skill: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> SkillResult[SkillCurrentStateView]:
    """
    查询下一次选择的精确版本与当前分支，不初始化待迁移状态。

    :param context (SkillApiContext): 用户认证上下文
    :param account_id (UUID): 指定账户
    :param scope (Literal["item", "account-directory"]): 对象范围
    :param skill (str | None): 单项来源
    :return SkillResult[SkillCurrentStateView]: 可用于预览或条件提交的当前状态
    """
    result = await command_service(context).selection.current(
        context.user.id, selector(account_id, scope, skill)
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.get("/diff")
async def current_diff(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    account_id: UUID,
    scope: Literal["item", "account-directory"] = "item",
    skill: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=4096)] = None,
) -> SkillResult[SkillCheckpointDiff]:
    """
    默认差异固定到当前有效分支，尚未初始化或已过期时明确失败。

    :param context (SkillApiContext): 用户认证上下文
    :param account_id (UUID): 指定账户
    :param scope (Literal["item", "account-directory"]): 对象范围
    :param skill (str | None): 单项来源
    :param limit (int): 差异页大小
    :param cursor (str | None): 上页末尾路径
    :return SkillResult[SkillCheckpointDiff]: 当前状态相对原始基线的差异
    """
    service = command_service(context)
    current = await service.selection.current(context.user.id, selector(account_id, scope, skill))
    head = current.precondition.directory_head_id
    if scope == "item":
        target = current.precondition.targets[0]
        if target.expired:
            raise SkillContentError("STATE_EXPIRED", "selected state requires reset or restore")
        head = target.head_checkpoint_id
    if head is None:
        raise SkillContentError("STATE_NOT_INITIALIZED", "selected state has not been initialized")
    result = await StateDiffService(service.queries, SkillLocalRepository(context.session)).diff(
        context.user.id, head, limit, cursor
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=result)


@router.post("/commands")
async def state_command(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    payload: SkillStateCommand,
) -> SkillResult[SkillStateCommandView]:
    """
    保存完整重置恢复事务或返回无写入预览，规则和用户库不被改变。

    :param context (SkillApiContext): 用户认证上下文
    :param payload (SkillStateCommand): 明确动作、范围和期望状态
    :return SkillResult[SkillStateCommandView]: 完整预览或原子发布结果
    """
    result = await command_service(context).execute(context.user.id, payload)
    await context.session.commit()
    return SkillResult(
        status=result.status,
        committed=not payload.dry_run,
        operation_id=result.operation_id,
        data=result,
    )


@router.get("/operations")
async def state_operation(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: Annotated[str, Query(min_length=1, max_length=128)],
) -> SkillResult[SkillStateCommandView]:
    """
    幂等回执查询返回最初已提交结果，不执行旧命令。

    :param context (SkillApiContext): 用户认证上下文
    :param key (str): 原始操作键
    :return SkillResult[SkillStateCommandView]: 原始结果
    """
    result = await command_service(context).operation(context.user.id, key)
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )


@router.get("/operations/{operation_id}")
async def state_operation_by_id(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    operation_id: UUID,
) -> SkillResult[SkillStateCommandView]:
    """
    显式操作身份查询仍限定当前用户，并保留最初发布结果。

    :param context (SkillApiContext): 用户认证上下文
    :param operation_id (UUID): 原始状态操作身份
    :return SkillResult[SkillStateCommandView]: 不可变已提交回执
    """
    result = await command_service(context).operation_by_id(context.user.id, operation_id)
    return SkillResult(
        status=result.status, committed=True, operation_id=result.operation_id, data=result
    )
