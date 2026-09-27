"""
为节点配置导入提供不依赖技能功能开关的新鲜所有权授权。
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_node, get_session
from agent_remote_server.context import get_request_id
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_imports import SkillImportAuthorizationResponse
from agent_remote_server.services.skills.config_import import SkillConfigImportGuard

router = APIRouter(prefix="/node-api/tasks", tags=["node-api"])


@router.get("/{task_id}/config-import-authorization")
async def authorize_config_import(
    task_id: str,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SkillImportAuthorizationResponse:
    """
    在节点写入前校验精确租约任务，并释放本次短期数据库锁。

    :param task_id (str): 外部任务身份
    :param node (Node): 已认证节点
    :param session (AsyncSession): 请求事务
    :return SkillImportAuthorizationResponse: 当前任务及目录绑定
    """
    data = await SkillConfigImportGuard(session).authorize(node.id, task_id)
    await session.commit()
    return SkillImportAuthorizationResponse(data=data, request_id=get_request_id())
