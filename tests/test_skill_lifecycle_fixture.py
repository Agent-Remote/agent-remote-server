"""
验证生命周期验收仅补充测试能力，保留真实认证、报告和普通安装入口。
"""

from pathlib import Path
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from skill_lifecycle_live_support import LifecycleReports, lifecycle_app
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from test_node_api import heartbeat_payload
from test_skill_content_service import database as database
from test_skill_library import library as library
from test_skill_lifecycle_live import prepare_fixture

from agent_remote_server.models import Node, NodeTask, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.schemas.skill_library import SkillAddRequest


async def test_lifecycle_fixture_requires_authentication_and_preserves_reports(
    takeover: TakeoverHarness, tmp_path: Path
) -> None:
    """
    真实 API 提交报告后才补充兼容能力，普通受理不预造任务和快照。

    :param takeover (TakeoverHarness): 未接管的独立账户
    :param tmp_path (Path): 私有配置目录
    """
    values = await prepare_fixture(takeover, tmp_path)
    reports = LifecycleReports()
    app = lifecycle_app(takeover, reports)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            request = SkillAddRequest(
                idempotency_key=str(uuid4()),
                expected_generation=0,
                items=(await takeover.library.candidate(),),
            )
            accepted = await client.post(
                "/api/v1/skills/installations",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + values["user_token"]},
            )
            assert accepted.status_code == 200
            assert accepted.json()["status"] == "preparing"
            path = "/api/v1/node-api/heartbeat"
            payload = heartbeat_payload(str(takeover.node))
            runtime = payload["runtime"]
            assert isinstance(runtime, dict)
            runtime["runtime_capabilities"] = {"backends": ["native"]}
            assert (await client.post(path, json=payload)).status_code == 401
            assert reports.heartbeats == reports.reconciliations == 0
            headers = {"Authorization": "Bearer " + values["token"]}
            response = await client.post(path, json=payload, headers=headers)
            assert response.status_code == 200
            response = await client.post(
                "/api/v1/node-api/reconcile",
                headers=headers,
                json={
                    "node_id": str(takeover.node),
                    "sections": ["runtime_sessions", "resources"],
                    "snapshot": {
                        "runtime": {"runtime_capabilities": {"backends": ["native"]}},
                        "sessions": [],
                    },
                },
            )
            assert response.status_code == 200
        assert reports.heartbeats == reports.reconciliations == 1
        async with takeover.library.database() as session:
            node = await session.get(Node, takeover.node)
            assert node is not None
            assert node.runtime_capabilities["backends"] == ["native"]
            assert "skill_manager" in node.runtime_capabilities
            for model in (NodeTask, Session, SessionSkillSnapshot):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        await app.state.database_engine.dispose()
