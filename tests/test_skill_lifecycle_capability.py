"""
验证隔离能力注入保留认证与真实后端报告，慢请求不会阻断心跳。
"""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from skill_lifecycle_live_support import LifecycleReports, lifecycle_app
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from test_skill_content_service import database as database
from test_skill_library import library as library
from test_skill_lifecycle_live import prepare_fixture

from agent_remote_server.models import Node


@pytest.mark.parametrize("empty_report", [False, True])
async def test_lifecycle_heartbeat_is_atomic_and_independent_of_slow_routes(
    takeover: TakeoverHarness, tmp_path: Path, empty_report: bool
) -> None:
    """
    并行慢请求不能阻断原生产心跳，测试补充能力不能伪造后端或绕过认证。

    :param takeover (TakeoverHarness): 隔离原账户和数据库
    :param tmp_path (Path): 私有临时配置目录
    :param empty_report (bool): 新版节点显式上报空能力表
    """
    values = await prepare_fixture(takeover, tmp_path)
    reports = LifecycleReports()
    app = lifecycle_app(takeover, reports)
    entered, release = asyncio.Event(), asyncio.Event()

    @app.get("/fixture-slow")
    async def slow_route() -> dict[str, bool]:
        """
        保持路由未完成，模拟长时间发布阶段。

        :return dict[str, bool]: 释放后的有界结果
        """
        entered.set()
        await release.wait()
        return {"complete": True}

    async with AsyncClient(transport=ASGITransport(app), base_url="http://fixture") as client:
        stalled = asyncio.create_task(client.get("/fixture-slow"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for backends in ([], ["native"]):
                payload = {
                    "node_id": values["node_id"],
                    "version": "test",
                    "supported_tool_types": ["claude"],
                    "resources": {
                        "cpu_load": 0,
                        "memory_used_bytes": 0,
                        "memory_total_bytes": 1,
                        "disk_used_bytes": 0,
                        "disk_total_bytes": 1,
                    },
                    "runtime": {
                        "docker_ok": False,
                        "tmux_ok": bool(backends),
                        "runtime_capabilities": {
                            "backends": backends,
                            **({"skill_manager": {}} if empty_report else {}),
                        },
                    },
                }
                response = await asyncio.wait_for(
                    client.post(
                        "/api/v1/node-api/heartbeat",
                        json=payload,
                        headers={"Authorization": "Bearer " + values["token"]},
                    ),
                    5,
                )
                assert response.status_code == 200
                assert not stalled.done()
                async with takeover.library.database() as session:
                    node = await session.get(Node, takeover.node)
                    assert node is not None
                    assert node.runtime_capabilities["backends"] == backends
                    assert "skill_manager" in node.runtime_capabilities
                rejected = await client.post("/api/v1/node-api/heartbeat", json=payload)
                assert rejected.status_code == 401
            assert reports.heartbeats == 2
            assert reports.responses["POST /node-api/heartbeat 200"] == 2
            assert reports.responses["POST /node-api/heartbeat 401"] == 2
        finally:
            release.set()
            await stalled
            await app.state.database_engine.dispose()
