"""
配置导入的旧任务仍须先证明当前租约，才能取得后端迁移状态诊断。
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_config_import import queued
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import NodeTask, ToolAccountProfile


@pytest.mark.parametrize("phase", ["leased", "pending", "failed", "expired"])
async def test_pending_backend_diagnostic_requires_current_import_lease(
    user_client: AsyncClient, stopped: RuntimeHarness, phase: str
) -> None:
    """
    有效旧任务得到稳定待恢复错误，未授权任务不能据此获知账户的当前迁移状态。

    :param user_client (AsyncClient): 真实用户及节点认证客户端
    :param stopped (RuntimeHarness): 独立账户和数据库
    :param phase (str): 当前租约或无效任务阶段
    """
    task_id, headers = await queued(user_client, stopped, ["~/.claude/settings.json"])
    async with stopped.database.begin() as session:
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        profile = await session.scalar(
            select(ToolAccountProfile).where(
                ToolAccountProfile.tool_account_id == stopped.account,
            )
        )
        assert task is not None and profile is not None
        profile.profile_json = {
            **profile.profile_json,
            "runtime_migration": {"status": "recovery_required"},
        }
        if phase == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        else:
            task.status = phase
    endpoint = f"/api/v1/node-api/tasks/{task_id}"
    for operation, response in (
        (
            "authorize",
            await user_client.get(endpoint + "/config-import-authorization", headers=headers),
        ),
        ("start", await user_client.post(endpoint + "/start", headers=headers)),
    ):
        if operation == "start" and phase == "failed":
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "COMMON_CONFLICT"
            continue
        assert response.status_code == (409 if phase == "leased" else 404)
        assert response.json()["error"]["code"] == (
            "RUNTIME_MIGRATION_PENDING" if phase == "leased" else "COMMON_NOT_FOUND"
        )
