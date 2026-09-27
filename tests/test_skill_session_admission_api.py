"""
验证真实 HTTP 凭据下的启动、租约内容访问、冲突解决和能力撤回闭环。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from test_node_api import heartbeat_payload
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_migration_conflicts import pending
from test_skill_migration_resolution_drafts import edit_request
from test_skill_session_admission import capability, ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice


async def payload(state: RuntimeHarness) -> dict[str, object]:
    """
    只提交现有会话 API 参数，客户端不选择快照、目录或运行后端。

    :param state (RuntimeHarness): 已授权测试账户
    :return dict[str, object]: 启动请求
    """
    async with state.database() as session:
        original = await session.get(Session, state.session)
        assert original is not None
        return {
            "tool_type": "claude",
            "tool_account_id": str(state.account),
            "workspace_id": str(original.workspace_id),
            "project_key": original.project_key,
            "argv": [],
        }


@pytest.mark.parametrize("tamper", ["snapshot_id", "protocol_version", "manifest_version"])
async def test_http_creation_only_grants_content_to_exact_leased_task(
    user_client: AsyncClient, stopped: RuntimeHarness, tamper: str
) -> None:
    """
    启动成功绑定可租约任务，未租约与用户凭据均不能借快照读取 Node 专用内容。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 已准备账户
    :param tamper (str): 下载前被篡改的任务指针字段
    """
    await ready(stopped)
    response = await user_client.post("/api/v1/sessions", json=await payload(stopped))
    assert response.status_code == 200
    identity = UUID(response.json()["data"]["id"])
    async with stopped.database() as session:
        snapshot = await session.scalar(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.session_id == identity)
        )
        assert snapshot is not None
        snapshot_id, task_id = snapshot.id, snapshot.prepare_task_id
    path = f"/api/v1/node/skill-snapshots/{snapshot_id}?task_id={task_id}"
    node_token = await token(stopped, stopped.owner, "node")
    headers = {"Authorization": f"Bearer {node_token}"}
    assert (await user_client.get(path)).status_code == 401
    assert (await user_client.get(path, headers=headers)).status_code == 404
    async with stopped.database.begin() as session:
        task = await session.get(NodeTask, task_id)
        assert task is not None
        task.status = "leased"
        task.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    content = await user_client.get(path, headers=headers)
    assert content.status_code == 200
    data = content.json()["data"]
    assert data["session_id"] == str(identity) and data["task_id"] == str(task_id)
    assert data["items"][0]["entry_name"] == "learning"
    async with stopped.database.begin() as session:
        task = await session.get(NodeTask, task_id)
        assert task is not None
        task.payload = {
            **task.payload,
            "skill_manager": {
                "protocol_version": 1,
                "manifest_version": 1,
                "snapshot_id": str(snapshot_id),
                "task_id": str(task_id),
                tamper: str(uuid4()) if tamper == "snapshot_id" else True,
            },
        }
        flag_modified(task, "payload")
    assert (await user_client.get(path, headers=headers)).status_code == 404


async def test_http_conflict_can_be_inspected_resolved_and_then_admitted(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    HTTP 拒绝启动后仍能检查原迁移，明确解决后新的启动使用已发布分支。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有内容卷
    """
    await pending(stopped, tmp_path, "forward")
    await ready(stopped)
    launch = await payload(stopped)
    response = await user_client.post("/api/v1/sessions", json=launch)
    assert response.status_code == 409
    error = response.json()["error"]
    assert (
        error["code"] == "STATE_MIGRATION_REQUIRED" and error["details"]["preparations_committed"]
    )
    identity = error["details"]["migration_ids"][0]
    base = f"/api/v1/skills/state/migration/conflicts/{identity}"
    assert (await user_client.get(base)).status_code == 200
    resolved = await user_client.post(
        base + "/resolve",
        json=edit_request(SkillResolutionChoice(use="current")).model_dump(mode="json"),
    )
    assert resolved.status_code == 200 and resolved.json()["data"]["status"] == "published"
    assert (await user_client.post("/api/v1/sessions", json=launch)).status_code == 200


@pytest.mark.parametrize("field,value", [("protocol_version", True), ("recovery", 1)])
async def test_malformed_heartbeat_replaces_previously_valid_capability(
    user_client: AsyncClient, stopped: RuntimeHarness, field: str, value: object
) -> None:
    """
    数字与布尔值的 JSON 相等不能让错误新报告保留旧能力和新的心跳有效期。

    :param user_client (AsyncClient): 实际 API 客户端
    :param stopped (RuntimeHarness): 原账户
    :param field (str): 畸形字段
    :param value (object): 不允许强制转换的值
    """
    await ready(stopped)
    node_token = await token(stopped, stopped.owner, "node")
    report = capability()
    report[field] = value
    heartbeat = heartbeat_payload(str(stopped.node))
    heartbeat["runtime"] = {
        "docker_ok": False,
        "tmux_ok": True,
        "runtime_capabilities": {"backends": ["native"], "skill_manager": {"native": report}},
    }
    response = await user_client.post(
        "/api/v1/node-api/heartbeat",
        headers={"Authorization": f"Bearer {node_token}"},
        json=heartbeat,
    )
    assert response.status_code == 200
    async with stopped.database() as session:
        node = await session.get(Node, stopped.node)
        assert node is not None and node.runtime_capabilities["skill_manager"] == {}
    rejected = await user_client.post("/api/v1/sessions", json=await payload(stopped))
    assert (
        rejected.status_code == 409
        and rejected.json()["error"]["code"] == "SKILL_MANAGER_UNSUPPORTED"
    )
