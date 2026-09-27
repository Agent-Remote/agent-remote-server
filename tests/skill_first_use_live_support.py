"""
为显式启用的首次接管验收启动真实 HTTP 服务及隔离 Linux Worker，不读取部署凭据。
"""

import asyncio
import json
import os
import signal
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from skill_takeover_support import TakeoverHarness
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import RequestResponseEndpoint

from agent_remote_server.api.deps import get_session
from agent_remote_server.main import create_app


@dataclass
class FirstUseServer:
    """
    保存临时服务地址及已实际注入的确认丢失次数。
    """

    local_url: str = ""
    worker_url: str = ""
    dropped: int = 0


def first_use_app(state: TakeoverHarness, observation: FirstUseServer, drop: bool) -> FastAPI:
    """
    仅替换数据库连接，在生产接管事务提交之后按需丢弃一次原响应。

    :param state (TakeoverHarness): 独立账户及内容卷
    :param observation (FirstUseServer): 本次网络观察
    :param drop (bool): 是否注入一次提交后响应丢失
    :return FastAPI: 使用真实认证与路由的应用
    """
    app = create_app(state.settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        为真实网络请求建立独立事务。

        :return AsyncIterator[AsyncSession]: 当前请求会话
        """
        async with state.library.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session

    @app.middleware("http")
    async def lose_confirmation(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        等待原生产处理结束后丢弃确认，不能替代真实上传或提交。

        :param request (Request): 当前网络请求
        :param call_next (RequestResponseEndpoint): 原生产路由
        :return Response: 原响应或一次确定的传输故障
        """
        response = await call_next(request)
        if (
            drop
            and observation.dropped == 0
            and request.method == "POST"
            and request.url.path.startswith("/api/v1/node/skill-takeovers/")
            and request.url.path.endswith("/complete")
            and response.status_code == 200
        ):
            observation.dropped += 1
            return JSONResponse({"detail": "disposable post-commit response loss"}, status_code=503)
        return response

    return app


@asynccontextmanager
async def serve_first_use(
    state: TakeoverHarness, drop: bool, application: FastAPI | None = None
) -> AsyncIterator[FirstUseServer]:
    """
    以真实监听套接字服务专用测试库，退出时关闭请求及额外连接池。

    :param state (TakeoverHarness): 独立测试数据库与配置
    :param drop (bool): 是否丢弃一次已提交接管响应
    :param application (FastAPI | None): 可选的独立验收应用
    :return AsyncIterator[FirstUseServer]: 本机和容器可达地址
    """
    observation = FirstUseServer()
    app = application if application is not None else first_use_app(state, observation, drop)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    task: asyncio.Task[None] | None = None
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off", ws="none")
    )
    try:
        listener.bind(("0.0.0.0", 0))
        port = listener.getsockname()[1]
        observation.local_url = f"http://127.0.0.1:{port}"
        observation.worker_url = f"http://host.docker.internal:{port}"
        task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("disposable first-use server exited before startup")
                await asyncio.sleep(0.01)
        yield observation
    finally:
        server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, 10)
            finally:
                listener.close()
                await app.state.database_engine.dispose()
        else:
            listener.close()
            await app.state.database_engine.dispose()


async def run_first_use_worker(repo: Path, fixture: Path, values: dict[str, str | bool]) -> None:
    """
    通过已有隔离脚本运行真实 Worker，失败时只显示不含凭据的子进程输出。

    :param repo (Path): 相邻 Node 仓库
    :param fixture (Path): 专用只读凭据文件
    :param values (dict[str, str | bool]): 一次性连接和原操作身份
    """
    with open(fixture, "x", encoding="utf-8", opener=private_fixture) as output:
        json.dump(values, output)
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(repo / "tests/linux_skill_first_use_test.sh"),
            cwd=repo,
            env=os.environ | {"AGENT_REMOTE_TEST_SKILL_FIRST_USE_FIXTURE": str(fixture)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        async with asyncio.timeout(300):
            output_bytes, _ = await process.communicate()
        output_text = output_bytes.decode("utf-8", errors="replace")
        for value in values.values():
            if isinstance(value, str) and value:
                output_text = output_text.replace(value, "<fixture>")
        assert process.returncode == 0, output_text[-12000:]
        assert "--- PASS: TestFirstUseWorkerLiveServer" in output_text, output_text[-12000:]
    finally:
        try:
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 30)
        finally:
            fixture.unlink(missing_ok=True)


def private_fixture(path: str, flags: int) -> int:
    """
    从首次创建开始限制临时令牌文件的权限。

    :param path (str): 新文件路径
    :param flags (int): 独占创建选项
    :return int: 私有文件描述符
    """
    return os.open(path, flags, 0o600)
