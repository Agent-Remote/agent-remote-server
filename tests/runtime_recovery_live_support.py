"""
为显式迁移恢复提供独立正式守护进程、私有 CLI 身份及受控 HTTP 回应丢失。
"""

import asyncio
import json
import os
import signal
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from live_acceptance_support import LiveDaemonProcess
from skill_first_use_live_support import FirstUseServer, first_use_app, private_fixture
from skill_takeover_support import TakeoverHarness
from starlette.middleware.base import RequestResponseEndpoint


@dataclass
class RecoveryReports:
    """
    仅记录无内容的路由计数和已提交回应丢失次数。
    """

    lose_replies: bool
    acceptance_lost: int = 0
    completion_lost: int = 0
    completion_rejected: int = 0
    routes: dict[str, int] = field(default_factory=dict)


def recovery_app(state: TakeoverHarness, reports: RecoveryReports) -> FastAPI:
    """
    使用生产认证与业务路由，注入提交前拒绝和提交后回应丢失。

    :param state (TakeoverHarness): 独立数据与身份
    :param reports (RecoveryReports): 有界无内容观察
    :return FastAPI: 本次隔离应用
    """
    app = first_use_app(state, FirstUseServer(), False)

    @app.middleware("http")
    async def observe(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        不替换生产任务、结果或授权，记录真实路由及受控故障。

        :param request (Request): 原始网络请求
        :param call_next (RequestResponseEndpoint): 原生产处理
        :return Response: 原回应或受控传输故障
        """
        path = request.url.path
        if (
            reports.lose_replies
            and request.method == "POST"
            and "/tasks/recover_tool_account_runtime:" in path
            and path.endswith("/complete")
            and reports.completion_rejected == 0
        ):
            reports.completion_rejected += 1
            return JSONResponse({"detail": "disposable pre-commit failure"}, status_code=503)
        response = await call_next(request)
        route = str(getattr(request.scope.get("route"), "path", "unmatched"))
        key = f"{request.method} {route} {response.status_code}"
        reports.routes[key] = reports.routes.get(key, 0) + 1
        if reports.lose_replies and request.method == "POST" and response.status_code == 200:
            if path.endswith("/runtime-migration/recover") and reports.acceptance_lost == 0:
                reports.acceptance_lost += 1
                return JSONResponse({"detail": "disposable committed reply loss"}, status_code=503)
            if (
                "/tasks/recover_tool_account_runtime:" in path
                and path.endswith("/complete")
                and reports.completion_lost == 0
            ):
                reports.completion_lost += 1
                return JSONResponse({"detail": "disposable committed reply loss"}, status_code=503)
        return response

    return app


@asynccontextmanager
async def run_recovery_node(
    repo: Path, fixture: Path, values: dict[str, str | bool]
) -> AsyncIterator[LiveDaemonProcess]:
    """
    启动正式 Worker 与 Helper 容器，凭据只进入一次性私有文件。

    :param repo (Path): 相邻 Node 仓库
    :param fixture (Path): 原始一次性凭据文件
    :param values (dict[str, str | bool]): 独立连接与身份
    :return AsyncIterator[LiveDaemonProcess]: 可观察的原进程
    """
    __tracebackhide__ = True
    with open(fixture, "x", encoding="utf-8", opener=private_fixture) as output:
        json.dump(values, output)
    process: asyncio.subprocess.Process | None = None
    reader: asyncio.Task[None] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(repo / "tests/linux_runtime_recovery_test.sh"),
            cwd=repo,
            env=os.environ | {"AGENT_REMOTE_TEST_RUNTIME_RECOVERY_FIXTURE": str(fixture)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        ready = asyncio.Event()
        reader = asyncio.create_task(read_recovery_output(process, values, ready))
        yield LiveDaemonProcess(process, reader, ready)
    finally:
        try:
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 40)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if reader is not None:
                await asyncio.gather(reader, return_exceptions=True)
        finally:
            fixture.unlink(missing_ok=True)


async def read_recovery_output(
    process: asyncio.subprocess.Process, values: dict[str, str | bool], ready: asyncio.Event
) -> None:
    """
    持续排空有界输出，令牌及原身份不进入失败显示。

    :param process (asyncio.subprocess.Process): 原脚本进程
    :param values (dict[str, str | bool]): 需要遮蔽的一次性输入
    :param ready (asyncio.Event): 真实重启准备事件
    """
    __tracebackhide__ = True
    assert process.stdout is not None
    lines: deque[str] = deque(maxlen=100)
    passed = False
    async with asyncio.timeout(600):
        while raw := await process.stdout.readline():
            text = raw.decode("utf-8", errors="replace")
            if "RECOVERY_DAEMONS_READY" in text:
                ready.set()
            if "--- PASS: TestPassiveRecoveryThroughProductionDaemons" in text:
                passed = True
            for value in values.values():
                if isinstance(value, str) and value:
                    text = text.replace(value, "<fixture>")
            lines.append(text[-2000:])
        await process.wait()
    assert process.returncode == 0 and passed, "".join(lines)[-12000:]


async def recovery_cli(
    binary: Path, root: Path, args: list[str]
) -> tuple[int, dict[str, object] | None, str]:
    """
    真正执行 CLI，保留类型化输出与有界错误，不通过宿主凭据回退。

    :param binary (Path): 当前正式 CLI
    :param root (Path): 专用配置与凭据根
    :param args (list[str]): 不含凭据的命令参数
    :return tuple[int, dict[str, object] | None, str]: 退出码、可选 JSON 和诊断
    """
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--json",
        "account",
        *args,
        env=os.environ | {"AGENT_REMOTE_HOME": str(root), "AGENT_REMOTE_SECRET_BACKEND": "file"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(45):
            output, error = await process.communicate()
        assert len(output) <= 64 << 10 and len(error) <= 64 << 10
        value = json.loads(output) if output else None
        assert value is None or isinstance(value, dict)
        return process.returncode or 0, value, error.decode("utf-8", errors="replace")
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
