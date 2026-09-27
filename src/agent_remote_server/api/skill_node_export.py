"""
提供登录用户的冻结导出授权及原 Node 的独立在线重验路由。
"""

from collections.abc import Callable, Coroutine
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_node,
    get_current_token,
    get_session,
    get_settings,
)
from agent_remote_server.api.skill_common import SkillApiContext, get_skill_context
from agent_remote_server.config import Settings
from agent_remote_server.models import AuthToken, Node
from agent_remote_server.schemas.skill_node_export import (
    NodeExportAuthorization,
    NodeExportPermission,
    NodeExportRenewal,
    NodeExportRequest,
    NodeExportVerification,
)
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.node_export import NodeExportService


class FrozenExportRoute(APIRoute):
    """
    冻结导出验证失败只返回有界诊断，不让框架回显含凭据的输入值。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        """
        保留原有类型和认证依赖，仅替换请求格式错误的公开诊断。

        :return Callable[[Request], Coroutine[object, object, Response]]: 不回显请求输入的处理器
        """
        handler = super().get_route_handler()

        async def sanitized(request: Request) -> Response:
            """
            避免默认验证错误把凭据字段纳入响应。

            :param request (Request): 当前导出请求
            :return Response: 原成功响应或统一的无输入诊断
            """
            try:
                return await handler(request)
            except RequestValidationError:
                raise SkillContentError(
                    "INVALID_REQUEST", "invalid frozen export request"
                ) from None

        return sanitized


router = APIRouter(
    prefix="/skills/state/node-exports", tags=["skill-state"], route_class=FrozenExportRoute
)
node_router = APIRouter(
    prefix="/node/skill-state-exports", tags=["node-skill-state"], route_class=FrozenExportRoute
)


@router.post("/{snapshot_id}/authorize")
async def authorize(
    snapshot_id: UUID,
    payload: NodeExportRequest,
    context: Annotated[SkillApiContext, Depends(get_skill_context)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> SkillResult[NodeExportAuthorization]:
    """
    原用户显式签发读取授权，确切 SSH 公钥同步不改变配置或内容保存状态。

    :param snapshot_id (UUID): 原始精确快照身份
    :param payload (NodeExportRequest): 设备和公钥选择
    :param context (SkillApiContext): 活跃登录用户及功能策略
    :param token (AuthToken): 必须持续活跃的原用户令牌
    :return SkillResult[NodeExportAuthorization]: 不宣称本地冻结存在的短期连接授权
    """
    result = await NodeExportService(context.session, context.settings).authorize(
        context.user.id, token.id, snapshot_id, payload
    )
    await context.session.commit()
    return SkillResult(status="authorized", committed=False, data=result)


@node_router.post("/{snapshot_id}/verify")
async def verify(
    snapshot_id: UUID,
    payload: NodeExportVerification,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SkillResult[NodeExportPermission]:
    """
    原节点在每次传输重验中确认全部活跃身份，不触碰启动租约或内容配额。

    :param snapshot_id (UUID): SSH 强制命令选择的原快照
    :param payload (NodeExportVerification): 原短期凭据及可信设备公钥身份
    :param node (Node): 当前 Node 令牌认证身份
    :param session (AsyncSession): 本次独立数据库观察
    :param settings (Settings): 当前功能与签名策略
    :return SkillResult[NodeExportPermission]: 当前仍有效的原冻结读取权限
    """
    result = await NodeExportService(session, settings).verify(node.id, snapshot_id, payload)
    return SkillResult(status="authorized", committed=False, data=result)


@node_router.post("/{snapshot_id}/renew")
async def renew(
    snapshot_id: UUID,
    payload: NodeExportVerification,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SkillResult[NodeExportRenewal]:
    """
    原节点在当前凭据到期前申请后继凭据，重新检查全部原始权限且不写入业务状态。

    :param snapshot_id (UUID): 原始快照身份
    :param payload (NodeExportVerification): 当前仍活跃的凭据及可信设备身份
    :param node (Node): 当前认证的来源节点
    :param session (AsyncSession): 本次独立数据库观察
    :param settings (Settings): 当前功能与签名策略
    :return SkillResult[NodeExportRenewal]: 同一原用户和快照的后继短期凭据
    """
    result = await NodeExportService(session, settings).renew(node.id, snapshot_id, payload)
    return SkillResult(status="authorized", committed=False, data=result)
