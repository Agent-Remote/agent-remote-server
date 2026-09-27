"""
在真实 PostgreSQL 独立连接上验证原键串行与确认期间保护变化。
"""

import asyncio
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_prune_commands import full_preview, service
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_prune import PruneCommand
from agent_remote_server.services.skills.content import SkillContentError


async def require_postgres(state: RuntimeHarness) -> None:
    """
    只有生产数据库真实行锁可以满足并发证明。

    :param state (RuntimeHarness): 测试数据库
    """
    async with state.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")


async def observe_blocked(state: RuntimeHarness, process: int) -> None:
    """
    查询数据库等待关系，不能用协程开始运行当成真正获得或等待用户锁。

    :param state (RuntimeHarness): 独立观察连接
    :param process (int): 正在请求用户锁的后端进程
    """
    async with state.database() as observer:
        for _ in range(100):
            if await observer.scalar(select(func.pg_blocking_pids(process))):
                return
            await asyncio.sleep(0.01)
    raise AssertionError("competing prune did not wait for the user lock")


@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("conflicting", [True, False])
async def test_same_key_waits_for_original_atomic_acceptance(
    stopped: RuntimeHarness,
    tmp_path: Path,
    commit: bool,
    conflicting: bool,
) -> None:
    """
    并发原键等待完整受理提交后重放；回滚则执行原计划，不产生第二份损失或任务。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param commit (bool): 首次完整受理是否最终提交
    :param conflicting (bool): 竞争请求是否使用不同原确认
    """
    await require_postgres(stopped)
    await shared_directory(stopped, tmp_path)
    secret = secrets.token_hex(32)
    last, _ = await full_preview(stopped, tmp_path, secret, limit=100)
    assert last.confirmation is not None
    request = PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation)
    second = request.model_copy(update={"confirmation": "different"}) if conflicting else request
    ready = asyncio.Event()
    processes: list[int] = []

    async def competitor() -> str:
        """
        独立事务以原请求等待，受理优先于读取已被第一次命令退役的内容。

        :return str: 原受理身份或明确业务拒绝
        """
        async with stopped.database.begin() as session:
            processes.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            try:
                return str(
                    (
                        await service(session, tmp_path, secret).execute(stopped.owner, second)
                    ).operation_id
                )
            except SkillContentError as error:
                return error.code

    task: asyncio.Task[str] | None = None
    try:
        async with stopped.database() as session:
            first = await service(session, tmp_path, secret).execute(stopped.owner, request)
            task = asyncio.create_task(competitor())
            await asyncio.wait_for(ready.wait(), timeout=2)
            await observe_blocked(stopped, processes[0])
            assert not task.done()
            if commit:
                await session.commit()
            else:
                await session.rollback()
    finally:
        if task is not None:
            outcome = await asyncio.wait_for(task, timeout=10)
    if conflicting:
        assert outcome == ("IDEMPOTENCY_CONFLICT" if commit else "INVALID_REQUEST")
    elif commit:
        assert outcome == str(first.operation_id)
    else:
        assert outcome != str(first.operation_id) and len(outcome) == 36


@pytest.mark.parametrize("commit", [True, False])
async def test_confirmation_waits_for_zero_reservation_upload(
    stopped: RuntimeHarness,
    tmp_path: Path,
    commit: bool,
) -> None:
    """
    原确认在新租约事务提交后拒绝全部动作，租约回滚后才允许执行。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param commit (bool): 租约是否真正提交
    """
    await require_postgres(stopped)
    await shared_directory(stopped, tmp_path)
    secret = secrets.token_hex(32)
    last, _ = await full_preview(stopped, tmp_path, secret, limit=100)
    assert last.confirmation is not None
    request = PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation)
    ready = asyncio.Event()
    processes: list[int] = []

    async def competitor() -> str:
        """
        确认请求只使用原末页凭据，不能重新选择较小计划。

        :return str: 已受理或明确前置条件失败
        """
        async with stopped.database.begin() as session:
            processes.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            try:
                await service(session, tmp_path, secret).execute(stopped.owner, request)
                return "accepted"
            except SkillContentError as error:
                return error.code

    task: asyncio.Task[str] | None = None
    try:
        async with stopped.database() as session:
            lease = await content_service(session, tmp_path).begin(
                stopped.owner,
                str(uuid4()),
                SkillTreeManifest(entries=(file_entry(b"historical", path="memory"),)),
                "state",
            )
            assert lease.reserved_bytes == 0
            task = asyncio.create_task(competitor())
            await asyncio.wait_for(ready.wait(), timeout=2)
            await observe_blocked(stopped, processes[0])
            if commit:
                await session.commit()
            else:
                await session.rollback()
    finally:
        if task is not None:
            outcome = await asyncio.wait_for(task, timeout=10)
    assert outcome == ("HEAD_CHANGED" if commit else "accepted")
