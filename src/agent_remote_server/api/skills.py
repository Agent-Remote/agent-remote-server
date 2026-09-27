"""
提供相互独立的用户库变更、覆盖规则和原始操作查询接口。
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.schemas.skill_diagnostics import SkillStorageView
from agent_remote_server.schemas.skill_effective import SessionSkillView
from agent_remote_server.schemas.skill_library import (
    SkillAddRequest,
    SkillRemoveRequest,
    SkillRollbackRequest,
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_results import (
    SkillInstallationView,
    SkillLibraryView,
    SkillLocalView,
    SkillMutationData,
    SkillResult,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.diagnostics import SkillDiagnosticService
from agent_remote_server.services.skills.effective_queries import SkillEffectiveQueryService
from agent_remote_server.services.skills.system_views import configured_systems

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("")
async def list_skills(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    tool: str | None = None,
    account_id: UUID | None = None,
    effective: bool = False,
    include_system: bool = False,
) -> SkillResult[SkillLibraryView]:
    """
    列出当前用户库，账户查询必须明确要求有效规则。

    :param context (SkillApiContext): 已认证上下文
    :param tool (str | None): 可选工具过滤
    :param account_id (UUID | None): 可选账户
    :param include_system (bool): 是否附加系统目录
    :param effective (bool): 是否解释有效规则
    :return SkillResult[SkillLibraryView]: 配置清单，不代表模型已经加载
    """
    if account_id is not None and not effective:
        raise SkillContentError("INVALID_REQUEST", "account list requires effective=true")
    scope = _query_scope(tool, account_id)
    data = await context.library().list_skills(context.user.id, scope=scope)
    if include_system or effective:
        data.system_items = configured_systems(context.settings, effective)
    if account_id is not None:
        service = SkillEffectiveQueryService(context.session)
        items: list[SkillInstallationView | SkillLocalView] = [*data.items, *data.local_items]
        for item in items:
            item.account_state = await service.account(context.user.id, account_id, item)

    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=data)


@router.post("/installations")
async def add_skills(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], payload: SkillAddRequest
) -> SkillResult[SkillMutationData]:
    """
    同一用户库事务原子安装全部选定项。

    :param context (SkillApiContext): 已认证上下文
    :param payload (SkillAddRequest): 完整安装计划
    :return SkillResult[SkillMutationData]: 持久化受理结果
    """
    return await context.execute(payload)


@router.post("/updates")
async def update_skill(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], payload: SkillUpdateRequest
) -> SkillResult[SkillMutationData]:
    """
    登记单项候选或激活版本，默认固定版本的账户不被改写。

    :param context (SkillApiContext): 已认证上下文
    :param payload (SkillUpdateRequest): 更新计划
    :return SkillResult[SkillMutationData]: 配置和目标结果
    """
    return await context.execute(payload)


@router.post("/rules")
async def change_skill_rule(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], payload: SkillRuleRequest
) -> SkillResult[SkillMutationData]:
    """
    修改指定范围的启用、pin 或继承字段。

    :param context (SkillApiContext): 已认证上下文
    :param payload (SkillRuleRequest): 规则修改计划
    :return SkillResult[SkillMutationData]: 保留覆盖说明及受理结果
    """
    return await context.execute(payload)


@router.post("/rollbacks")
async def rollback_skill(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], payload: SkillRollbackRequest
) -> SkillResult[SkillMutationData]:
    """
    从服务器保留包和真实激活历史回滚默认版本。

    :param context (SkillApiContext): 已认证上下文
    :param payload (SkillRollbackRequest): 回滚选择
    :return SkillResult[SkillMutationData]: 回滚受理结果
    """
    return await context.execute(payload)


@router.post("/removals")
async def remove_skill(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], payload: SkillRemoveRequest
) -> SkillResult[SkillMutationData]:
    """
    逻辑卸载当前安装，保留独立版本和纪元引用。

    :param context (SkillApiContext): 已认证上下文
    :param payload (SkillRemoveRequest): 卸载计划
    :return SkillResult[SkillMutationData]: 归档受理结果
    """
    return await context.execute(payload)


@router.get("/operations")
async def find_skill_operation(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    key: str,
) -> SkillResult[SkillMutationData]:
    """
    根据客户端持久化幂等键恢复因网络断开未收到的受理结果。

    :param context (SkillApiContext): 已认证上下文
    :param key (str): 原始幂等键
    :return SkillResult[SkillMutationData]: 原始受理状态
    """
    return await context.library().status_by_key(context.user.id, key)


@router.get("/operations/{operation_id}")
async def skill_status(
    context: Annotated[SkillApiContext, Depends(get_skill_context)], operation_id: UUID
) -> SkillResult[SkillMutationData]:
    """
    查询当前用户原始操作，等待断开不会撤销该记录。

    :param context (SkillApiContext): 已认证上下文
    :param operation_id (UUID): 原始操作 ID
    :return SkillResult[SkillMutationData]: 原始操作状态
    """
    return await context.library().status(context.user.id, operation_id)


@router.get("/installations/{identifier}")
async def skill_info(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    identifier: str,
    tool: str | None = None,
    account_id: UUID | None = None,
) -> SkillResult[SkillInstallationView | SkillLocalView]:
    """
    用稳定 ID 查询归档来源，或用名称查询当前安装。

    :param context (SkillApiContext): 已认证上下文
    :param identifier (str): 名称或稳定 ID
    :param tool (str | None): 可选工具解释范围
    :param account_id (UUID | None): 可选账户解释范围
    :return SkillResult[SkillInstallationView | SkillLocalView]: 完整版本和逐字段规则说明
    """
    data = await context.library().info(
        context.user.id, identifier, scope=_query_scope(tool, account_id)
    )
    if account_id is not None:
        data.account_state = await SkillEffectiveQueryService(context.session).account(
            context.user.id, account_id, data
        )
    diagnostics = SkillDiagnosticService(context.session, context.settings.skill_storage_policy)
    data.storage = await diagnostics.storage_view(context.user.id)
    kind: Literal["local_revision", "revision"] = (
        "local_revision" if isinstance(data, SkillLocalView) else "revision"
    )
    histories = await diagnostics.histories(
        context.user.id, kind, tuple(row.id for row in data.revisions)
    )
    for revision in data.revisions:
        revision.retention = histories[revision.id]
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=data)


def _query_scope(tool: str | None, account_id: UUID | None) -> SkillScope | None:
    """
    查询最多解释一个工具或账户，拒绝互斥范围并用。

    :param tool (str | None): 工具类型
    :param account_id (UUID | None): 账户标识
    :return SkillScope | None: 明确的可选范围
    """
    if tool is not None and account_id is not None:
        raise SkillContentError("INVALID_REQUEST", "tool and account scopes are mutually exclusive")
    if tool is None and account_id is None:
        return None
    return SkillScope(tools=(tool,) if tool is not None else (), account_id=account_id)


@router.get("/storage")
async def storage_status(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
) -> SkillResult[SkillStorageView]:
    """
    查询用户实际配额、预留和独立物理删除汇总，不猜测节点磁盘。

    :param context (SkillApiContext): 认证请求上下文
    :return SkillResult[SkillStorageView]: 只读当前观察
    """
    data = await SkillDiagnosticService(
        context.session, context.settings.skill_storage_policy
    ).storage_view(context.user.id)
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=data)


@router.get("/sessions/{session_id}")
async def session_skills(
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    session_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> SkillResult[SessionSkillView]:
    """
    读取本用户原会话快照，不为旧会话重新解析当前配置。

    :param context (SkillApiContext): 认证上下文
    :param session_id (UUID): 原始会话身份
    :param limit (int): 有界页大小
    :param cursor (str | None): 同快照原成员名称
    :return SkillResult[SessionSkillView]: 完整身份上下文和有界原成员页
    """
    data = await SkillEffectiveQueryService(context.session).session(
        context.user.id, session_id, limit, cursor
    )
    await context.session.commit()
    return SkillResult(status="ready", committed=False, data=data)
