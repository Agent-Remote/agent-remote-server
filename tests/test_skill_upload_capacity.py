"""
通过真实 HTTP 与 PostgreSQL 验证默认十万独立对象的完整收尾容量。
"""

import asyncio
import os
import socket
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import Request, Response
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import RequestResponseEndpoint
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.api.deps import get_session
from agent_remote_server.config import Settings
from agent_remote_server.main import create_app
from agent_remote_server.models import AuthToken, Node
from agent_remote_server.models.skill_storage import SkillContentObject, SkillUploadObject
from agent_remote_server.schemas.skill_finalizations import SkillFinalizationRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.security.tokens import hash_token

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_UPLOAD_CAPACITY") != "1",
    reason="requires explicit disposable PostgreSQL and real 100000-object HTTP transfer",
)


@asynccontextmanager
async def capacity_client(
    state: RuntimeHarness, root: Path, *, user_authenticated: bool = False
) -> AsyncIterator[AsyncClient]:
    """
    真实监听回环 HTTP，只有数据库工厂替换为本次隔离事务。

    :param state (RuntimeHarness): 原始已停止快照
    :param root (Path): 本次私有内容卷
    :param user_authenticated (bool): 包上传使用真实用户令牌，其余使用节点认证
    :return AsyncIterator[AsyncClient]: 使用真实认证的网络客户端
    """
    token = "capacity-test-" + uuid4().hex
    settings = Settings(
        secret_key="capacity-test-only",
        log_level="CRITICAL",
        skill_manager_enabled=True,
        skill_storage_root=root / "objects",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    async with state.database.begin() as session:
        assert session.get_bind().dialect.name == "postgresql", "requires disposable PostgreSQL"
        if user_authenticated:
            session.add(
                AuthToken(
                    user_id=state.owner,
                    token_type="user",
                    status="active",
                    token_hash=hash_token(settings.secret_key, token),
                    expires_at=datetime.now(UTC) + timedelta(hours=4),
                )
            )
        else:
            await session.execute(
                update(Node)
                .where(Node.id == state.node)
                .values(node_token_hash=hash_token(settings.secret_key, token))
            )
    app = create_app(settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        每个真实请求持有独立事务，失败由上下文回滚。

        :return AsyncIterator[AsyncSession]: 当前请求的数据库会话
        """
        async with state.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session

    @app.middleware("http")
    async def report_capacity_failure(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """
        仅输出异常类型与源码位置用于大规模失败定位，不记录参数或请求内容。

        :param request (Request): 当前验收请求
        :param call_next (RequestResponseEndpoint): 正式路由处理器
        :return Response: 未改变的正式响应
        """
        try:
            return await call_next(request)
        except Exception as error:
            print("capacity_exception=" + type(error).__name__, flush=True)
            for frame in traceback.extract_tb(error.__traceback__):
                print(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}", flush=True)
            if error.__cause__ is not None:
                print("capacity_cause=" + type(error.__cause__).__name__, flush=True)
            raise

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off", ws="none")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("capacity HTTP server exited before readiness")
                await asyncio.sleep(0.01)
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
            headers={"Authorization": "Bearer " + token},
            timeout=1200,
        ) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 15)
        finally:
            listener.close()
            await app.state.database_engine.dispose()


async def test_default_100000_distinct_objects_over_http(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    十万唯一文件全部经过正式节点路由和字节存储，完成后核对对象与索引生命周期。

    :param stopped (RuntimeHarness): 正式预约后已停止的原始快照
    :param tmp_path (Path): 本次实际内容卷
    """
    count = 100_000
    contents = [f"capacity-object-{index:06d}\n".encode() for index in range(count)]
    entries = tuple(
        file_entry(value, path=f"state-{index:06d}") for index, value in enumerate(contents)
    )
    manifest = SkillTreeManifest(entries=entries)
    assert len({entry.sha256 for entry in entries}) == count
    payload = SkillFinalizationRequest(
        session_id=stopped.session, idempotency_key=str(uuid4()), manifest=manifest, unclean=False
    )
    async with capacity_client(stopped, tmp_path) as client:
        start = time.monotonic()
        response = await client.post(
            f"/api/v1/node/skill-snapshots/{stopped.snapshot}/finalization",
            json=payload.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        plan = response.json()["data"]
        base = f"/api/v1/node/skill-finalizations/{plan['id']}"
        params = {"upload_id": plan["upload_id"]}
        print(f"capacity_begin_seconds={time.monotonic() - start:.3f}", flush=True)
        async with stopped.database() as session:
            engine = session.get_bind()
        full_manifest_reads = 0

        def observe_sql(
            connection: Connection,
            cursor: object,
            statement: str,
            parameters: object,
            context: ExecutionContext,
            executemany: bool,
        ) -> None:
            """
            只统计大清单读取次数，不保存 SQL 参数、身份令牌或内容。

            :param connection (Connection): 当前底层连接
            :param cursor (object): 数据库游标
            :param statement (str): 已生成 SQL 模板
            :param parameters (object): 不读取的绑定参数
            :param context (ExecutionContext): 当前执行上下文
            :param executemany (bool): 是否批量执行
            """
            nonlocal full_manifest_reads
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "skill_content_uploads.manifest_json" in statement
            ):
                full_manifest_reads += 1

        event.listen(engine, "before_cursor_execute", observe_sql)
        start = time.monotonic()
        try:
            async with asyncio.timeout(3 * 60 * 60):
                for index, (entry, content) in enumerate(zip(entries, contents, strict=True), 1):
                    response = await client.put(
                        base + "/files/" + entry.sha256, params=params, content=content
                    )
                    assert response.status_code == 200, (index, response.text)
                    assert not response.json()["committed"]
                    if index % 1_000 == 0:
                        print(
                            f"capacity_objects={index} "
                            f"elapsed_seconds={time.monotonic() - start:.3f}",
                            flush=True,
                        )
        finally:
            event.remove(engine, "before_cursor_execute", observe_sql)
        assert full_manifest_reads == 0
        print(
            f"capacity_object_seconds={time.monotonic() - start:.3f} "
            f"manifest_reads={full_manifest_reads}",
            flush=True,
        )
        start = time.monotonic()
        response = await client.post(base + "/complete", params=params)
        assert response.status_code == 200, response.text
        assert response.json()["committed"] and response.json()["status"] == "persisted"
        print(f"capacity_complete_seconds={time.monotonic() - start:.3f}", flush=True)
        async with stopped.database() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(SkillUploadObject)
                    .where(SkillUploadObject.upload_id == UUID(plan["upload_id"]))
                )
                == 0
            )
            stored_count = await session.scalar(
                select(func.count())
                .select_from(SkillContentObject)
                .where(
                    SkillContentObject.user_id == stopped.owner,
                    SkillContentObject.category == "state",
                )
            )
            assert stored_count is not None and stored_count >= count
