"""
为真实 Claude 生命周期提供隔离应用、显式测试能力和受控容器进程。
"""

import asyncio
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from skill_first_use_live_support import FirstUseServer, first_use_app, private_fixture
from skill_lifecycle_capability import LifecycleCapabilityMiddleware
from skill_takeover_support import TakeoverHarness
from starlette.middleware.base import RequestResponseEndpoint


@dataclass
class LifecycleReports:
    """
    统计真实守护进程报告及仅在验收库中补充能力的次数。
    """

    heartbeats: int = 0
    reconciliations: int = 0
    responses: dict[str, int] = field(default_factory=dict)


def lifecycle_app(state: TakeoverHarness, reports: LifecycleReports) -> FastAPI:
    """
    保留真实认证和路由，在专用库中显式补充尚未发布的测试能力。

    :param state (TakeoverHarness): 独立数据库与内容存储
    :param reports (LifecycleReports): 守护进程实际报告计数
    :return FastAPI: 不启用生产能力上报的验收应用
    """
    app = first_use_app(state, FirstUseServer(), False)
    app.add_middleware(LifecycleCapabilityMiddleware)

    @app.middleware("http")
    async def fixture_capability(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        统计真实响应，不阻塞并行心跳或伪造运行状态与收尾回执。

        :param request (Request): 实际网络请求
        :param call_next (RequestResponseEndpoint): 原生产路由
        :return Response: 未替换的原路由响应
        """
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        key = f"{request.method} {route} {response.status_code}"
        reports.responses[key] = reports.responses.get(key, 0) + 1
        if request.method != "POST" or response.status_code != 200:
            return response
        if request.url.path == "/api/v1/node-api/heartbeat":
            reports.heartbeats += 1
        elif request.url.path == "/api/v1/node-api/reconcile":
            reports.reconciliations += 1
        return response

    return app


async def run_lifecycle_worker(
    repo: Path, fixture: Path, values: dict[str, str], *, learning: bool = True
) -> None:
    """
    运行正式二进制验收并拥有整个子进程组，凭据只进入临时私有文件。

    :param repo (Path): 相邻 Node 仓库
    :param fixture (Path): 一次性连接文件
    :param values (dict[str, str]): 测试身份与短期认证信息
    :param learning (bool): 是否执行需要显式凭据的真实学习
    """
    __tracebackhide__ = True
    with open(fixture, "x", encoding="utf-8", opener=private_fixture) as output:
        json.dump(values, output)
    process: asyncio.subprocess.Process | None = None
    try:
        script = repo / "tests/linux_skill_lifecycle_test.sh"
        # 固定脚本源码，避免运行期间编辑工作区改变凭据清理流程。
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            script.read_text(encoding="utf-8"),
            str(script),
            cwd=repo,
            env=os.environ
            | {
                "AGENT_REMOTE_TEST_SKILL_LIFECYCLE_FIXTURE": str(fixture),
                "AGENT_REMOTE_TEST_SKILL_LIFECYCLE_MODE": "learning" if learning else "runtime",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        async with asyncio.timeout(1200):
            raw_output, _ = await process.communicate()
        text = raw_output.decode("utf-8", errors="replace")
        for value in values.values():
            if value:
                text = text.replace(value, "<fixture>")
        assert process.returncode == 0, text[-12000:]
        expected = "TestNativeClaudeLifecycle" if learning else "TestNativeDaemonLifecycle"
        assert f"--- PASS: {expected}" in text, text[-12000:]
    finally:
        try:
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 40)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        finally:
            fixture.unlink(missing_ok=True)
