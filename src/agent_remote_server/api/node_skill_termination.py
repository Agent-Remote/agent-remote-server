"""
接收原节点对单个受管快照的精确终止观察。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_node,
    get_device_relay_hub,
    get_ego_browser_revocation_bus,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.ego_browser.relay import EgoBrowserRevocationPublisher
from agent_remote_server.models import Node
from agent_remote_server.schemas.skill_results import SkillResult
from agent_remote_server.schemas.skill_terminations import (
    SkillCapturePendingRequest,
    SkillTerminationRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.termination import SkillTerminationService

router = APIRouter(prefix="/node", tags=["node-skill-termination"])


@router.post("/skill-snapshots/{snapshot_id}/termination")
async def observe_termination(
    snapshot_id: UUID,
    payload: SkillTerminationRequest,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    publisher: Annotated[EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)],
) -> SkillResult[SkillTerminationRequest]:
    """
    事务提交后原样回显停止依据，不宣称内容已经保存。

    :param snapshot_id (UUID): 原始快照身份
    :param payload (SkillTerminationRequest): 完整冻结输入的终止观察
    :param node (Node): 原始认证节点
    :param session (AsyncSession): 当前请求事务
    :param settings (Settings): 部署配置
    :param relay_hub (DeviceRelayHub): 设备连接关闭设施
    :param publisher (EgoBrowserRevocationPublisher): 浏览器撤销发布设施
    :return SkillResult[SkillTerminationRequest]: 已提交的原样停止收据
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    await SkillTerminationService(session, settings, relay_hub, publisher).observe(
        node.id, snapshot_id, payload
    )
    return SkillResult(status="stopped", committed=True, data=payload)


@router.post("/skill-snapshots/{snapshot_id}/capture-pending")
async def observe_capture_pending(
    snapshot_id: UUID,
    payload: SkillCapturePendingRequest,
    node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    relay_hub: Annotated[DeviceRelayHub, Depends(get_device_relay_hub)],
    publisher: Annotated[EgoBrowserRevocationPublisher, Depends(get_ego_browser_revocation_bus)],
) -> SkillResult[SkillCapturePendingRequest]:
    """
    独立确认已停止的写入者和固定捕获故障，不宣称内容已经冻结。

    :param snapshot_id (UUID): 原始快照身份
    :param payload (SkillCapturePendingRequest): 未完成冻结的精确终止观察
    :param node (Node): 原始认证节点
    :param session (AsyncSession): 当前请求事务
    :param settings (Settings): 部署配置
    :param relay_hub (DeviceRelayHub): 设备连接关闭设施
    :param publisher (EgoBrowserRevocationPublisher): 浏览器撤销发布设施
    :return SkillResult[SkillCapturePendingRequest]: 已提交的原样停止收据
    """
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    await SkillTerminationService(session, settings, relay_hub, publisher).observe(
        node.id, snapshot_id, payload
    )
    return SkillResult(status="stopped", committed=True, data=payload)
