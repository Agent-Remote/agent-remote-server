"""
验证真实命令释放、重建引用与失败回滚时的持久化历史时钟。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import versions
from test_skill_preparation import pin
from test_skill_resolution_service import pending
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute

from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_retention import (
    RetentionIndex,
    SkillRetentionRepository,
)
from agent_remote_server.schemas.skill_library import (
    SkillRemoveRequest,
    SkillRuleRequest,
    SkillScope,
)
from agent_remote_server.services.skills.retention import SkillRetentionInspector
from agent_remote_server.services.skills.retention.clocks import history_records, retention_mutation
from agent_remote_server.skill_manager.retention.graph import RetentionKey


async def clocks(state: RuntimeHarness) -> dict[RetentionKey, datetime | None]:
    """
    独立连接读取已提交时钟，不从创建时间计算缺失证据。

    :param state (RuntimeHarness): 用户及账户测试身份
    :return dict[RetentionKey, datetime | None]: 标准化为 UTC 的原始释放时间
    """
    async with state.database() as session:
        index = await SkillRetentionRepository(session).load(state.owner)
        return {
            key: row.retention_released_at.replace(tzinfo=UTC)
            if row.retention_released_at is not None
            else None
            for key, row in history_records(index).items()
        }


async def test_last_protection_release_and_pin_reacquisition(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    默认切换不能越过待收尾保护，最终发布启动时钟，重新 pin 清空再释放重启。

    :param stopped (RuntimeHarness): 有待收尾快照的账户
    :param tmp_path (Path): 内容卷
    """
    old_checkpoint = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    assert old_checkpoint is not None
    key = RetentionKey("checkpoint", str(old_checkpoint))
    source, _ = await versions(stopped, tmp_path)
    assert (await clocks(stopped))[key] is None
    receipt = await ingest(stopped, tmp_path, {})
    assert (await clocks(stopped))[key] is None
    before = datetime.now(UTC)
    await publish(stopped, tmp_path, receipt)
    first = (await clocks(stopped))[key]
    assert first is not None and before <= first <= datetime.now(UTC)
    await pin(stopped, tmp_path, source)
    assert (await clocks(stopped))[key] is None
    await pin(stopped, tmp_path, None)
    second = (await clocks(stopped))[key]
    assert second is not None and second > first


async def test_preview_replay_and_unknown_legacy_history_do_not_start_clocks(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    旧历史缺少释放证据时保持未知，预览、只读检查和重复受理不推进现有时钟。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    request = await command(stopped, tmp_path)
    previous = request.expected.targets[0].head_checkpoint_id
    assert previous is not None
    await execute(stopped, tmp_path, request)
    initial = await clocks(stopped)
    assert initial[RetentionKey("checkpoint", str(previous))] is not None
    await execute(stopped, tmp_path, request)
    await execute(stopped, tmp_path, await command(stopped, tmp_path, dry_run=True))
    async with stopped.database.begin() as session:
        await SkillRetentionInspector(session).inspect(stopped.owner)
    assert await clocks(stopped) == initial
    async with stopped.database.begin() as session:
        checkpoint = await session.get(SkillCheckpoint, previous)
        assert checkpoint is not None
        checkpoint.retention_released_at = None
        checkpoint.created_at = datetime.now(UTC) - timedelta(days=400)
    await pin(stopped, tmp_path, request.expected.targets[0].revision_id)
    assert (await clocks(stopped))[RetentionKey("checkpoint", str(previous))] is None


async def test_outer_rollback_restores_head_and_clock(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    服务保存点成功不等于请求提交，外层异常同时撤销 head 和历史释放证据。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    source, _ = await versions(stopped, tmp_path)
    previous = await clocks(stopped)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    generation = await library.generation()
    request = SkillRuleRequest(
        command="pin",
        skill="learning",
        revision=str(source),
        scope=SkillScope(account_id=stopped.account),
        expected_generation=generation,
        idempotency_key=str(uuid4()),
    )
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with stopped.database.begin() as session:
            await library.service(session).execute(stopped.owner, request)
            raise RuntimeError("outer rollback")
    assert await clocks(stopped) == previous
    assert await library.generation() == generation
    await library.execute(request)
    assert await clocks(stopped) != previous


async def test_failed_clock_analysis_rolls_back_business_savepoint(
    stopped: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    末尾引用分析失败时，即使调用方捕获异常并提交外层也不能保存部分业务变化。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入单次分析失败
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    previous = await clocks(stopped)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    info = await library.info()
    generation = await library.generation()
    original = SkillRetentionRepository.load
    calls = 0

    async def fail_second(self: SkillRetentionRepository, user_id: UUID) -> RetentionIndex:
        """
        在业务实际改变之后模拟无法取得完整引用索引。

        :param user_id (UUID): 当前用户
        :return RetentionIndex: 原始索引或测试错误
        """
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("analysis failure")
        return await original(self, user_id)

    with monkeypatch.context() as patch:
        patch.setattr(SkillRetentionRepository, "load", fail_second)
        async with stopped.database.begin() as session:
            with pytest.raises(RuntimeError, match="analysis failure"):
                await library.service(session).execute(
                    stopped.owner,
                    SkillRemoveRequest(
                        skill=str(info.id),
                        expected_generation=generation,
                        idempotency_key=str(uuid4()),
                    ),
                )
    assert await clocks(stopped) == previous
    assert not (await library.info()).removed
    assert await library.generation() == generation


async def test_nested_rollback_and_context_reuse(stopped: RuntimeHarness, tmp_path: Path) -> None:
    """
    内层失败由自身保存点撤销，外层继续时不会留下错误活动标记或提前释放。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    info = await library.info()
    previous = await clocks(stopped)
    async with stopped.database.begin() as session, retention_mutation(session, stopped.owner):
        with pytest.raises(RuntimeError, match="nested rollback"):
            async with retention_mutation(session, stopped.owner):
                item = await session.get(SkillInstallation, info.id)
                assert item is not None
                item.removed = True
                await session.flush()
                raise RuntimeError("nested rollback")
    assert await clocks(stopped) == previous
    await library.execute(
        SkillRemoveRequest(
            skill=str(info.id),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    assert (await clocks(stopped))[
        RetentionKey("revision", str(info.default_revision_id))
    ] is not None


async def test_conflict_release_records_complete_inputs_at_reset(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    未解决比较及其收尾快照没有时钟，reset 解除完整冲突保护后同次记录时间。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    conflict = await pending(stopped, tmp_path)
    key = RetentionKey("publication", str(conflict.id))
    receipt = RetentionKey("finalization", str(conflict.finalization_id))
    before = await clocks(stopped)
    assert before[key] is None and before[receipt] is None
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    after = await clocks(stopped)
    assert after[key] is not None and after[key] == after[receipt]


@pytest.mark.parametrize("outcome", ["complete", "fail", "reconcile"])
async def test_session_terminal_results_respect_retention_protection(
    stopped: RuntimeHarness, tmp_path: Path, outcome: str
) -> None:
    """
    已核验停止重新释放历史，失败回报和通用清单不能解除受管快照保护。

    :param stopped (RuntimeHarness): 原始终态会话
    :param tmp_path (Path): 私有内容卷
    :param outcome (str): 既有终态写入方式
    """
    from agent_remote_server.config import Settings
    from agent_remote_server.models import Node, Session, User
    from agent_remote_server.models.skill_snapshots import SkillFinalization
    from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
    from agent_remote_server.services.nodes import NodeService
    from agent_remote_server.services.sessions import ToolSessionService
    from agent_remote_server.services.skills.content import SkillContentError

    receipt_id = await ingest(stopped, tmp_path, {})
    await publish(stopped, tmp_path, receipt_id)
    result: dict[str, object] = {}
    if outcome == "complete":
        async with stopped.database.begin() as session:
            receipt = await session.get(SkillFinalization, receipt_id)
            assert receipt is not None
            session.add(
                SkillSnapshotTermination(
                    snapshot_id=stopped.snapshot,
                    incoming_digest=receipt.incoming_digest,
                    unclean=receipt.unclean,
                )
            )
            result = {
                "status": "stopped",
                "session_id": str(stopped.session),
                "runtime_backend": "native",
                "skill_finalization_operation_id": str(stopped.snapshot),
                "incoming_digest": receipt.incoming_digest,
                "unclean": receipt.unclean,
            }
    key = RetentionKey("snapshot", str(stopped.snapshot))
    first = (await clocks(stopped))[key]
    assert first is not None
    async with stopped.database.begin() as session, retention_mutation(session, stopped.owner):
        runtime = await session.get(Session, stopped.session)
        assert runtime is not None
        runtime.status = "running" if outcome == "reconcile" else "interrupted"
    if outcome != "reconcile":
        async with stopped.database() as session:
            user = await session.get(User, stopped.owner)
            assert user is not None
            await ToolSessionService(session, Settings()).stop_session(
                user=user, session_id=stopped.session
            )
    assert (await clocks(stopped))[key] is None
    async with stopped.database() as session:
        node = await session.get(Node, stopped.node)
        assert node is not None
        service = NodeService(session, Settings())
        if outcome == "reconcile":
            await service.reconcile(
                node=node, node_id=node.id, sections=["runtime_sessions"], snapshot={"sessions": []}
            )
        else:
            task_id = f"stop_tool_session:{stopped.session}"
            if outcome == "complete":
                await service.complete_task(node=node, task_id=task_id, result=result)
            else:
                with pytest.raises(SkillContentError):
                    await service.fail_task(node=node, task_id=task_id, error={})
    second = (await clocks(stopped))[key]
    if outcome in {"fail", "reconcile"}:
        assert second is None
    else:
        assert second is not None and second > first


async def test_history_deadline_uses_release_time_and_archived_epoch(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    普通历史按真实释放加三十天，卸载及重装后旧纪元按九十天，查询不改写时钟。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 私有内容卷
    """
    from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy

    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    head = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    key = RetentionKey("checkpoint", str(head))
    await versions(stopped, tmp_path)
    initial = await clocks(stopped)
    released = initial[key]
    assert released is not None
    async with stopped.database.begin() as session:
        rows = await SkillRetentionInspector(session).history(stopped.owner, SkillStoragePolicy())
        row = next(item for item in rows if item.key == key)
        assert not row.archived and not row.reasons
        assert row.expires_at == released + timedelta(days=30)
    assert await clocks(stopped) == initial
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    async with stopped.database() as session:
        rows = await SkillRetentionInspector(session).history(stopped.owner, SkillStoragePolicy())
        row = next(item for item in rows if item.key == key)
        assert row.archived and row.expires_at == released + timedelta(days=90)
    await library.add(await library.candidate())
    async with stopped.database() as session:
        rows = await SkillRetentionInspector(session).history(
            stopped.owner, SkillStoragePolicy(history_days=7, archive_days=120)
        )
        row = next(item for item in rows if item.key == key)
        assert row.archived and row.released_at == released
        assert row.expires_at == released + timedelta(days=120)


async def test_stop_interrupted_session_reacquires_history(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已完成收尾的 interrupted 会话进入 stopping 时重建保护，不能继续展示到期资格。

    :param stopped (RuntimeHarness): 原始已停止会话
    :param tmp_path (Path): 私有内容卷
    """
    from agent_remote_server.config import Settings
    from agent_remote_server.models import Session, User
    from agent_remote_server.services.sessions import ToolSessionService

    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    key = RetentionKey("snapshot", str(stopped.snapshot))
    assert (await clocks(stopped))[key] is not None
    async with stopped.database.begin() as session:
        tool_session = await session.get(Session, stopped.session)
        assert tool_session is not None
        tool_session.status = "interrupted"
    async with stopped.database() as session:
        user = await session.get(User, stopped.owner)
        assert user is not None
        await ToolSessionService(session, Settings()).stop_session(
            user=user, session_id=stopped.session
        )
    assert (await clocks(stopped))[key] is None


@pytest.mark.parametrize("commit_first", [True, False])
async def test_postgres_reference_race_preserves_last_release(
    stopped: RuntimeHarness, tmp_path: Path, commit_first: bool
) -> None:
    """
    独立写连接等待原引用事务，提交后重新释放计时，回滚后重复解除不改变旧时钟。

    :param stopped (RuntimeHarness): 已停止账户
    :param tmp_path (Path): 内容卷
    :param commit_first (bool): 第一份重新保护事务是否提交
    """
    import asyncio

    from sqlalchemy import func, select

    async with stopped.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {}))
    head = (await command(stopped, tmp_path)).expected.targets[0].head_checkpoint_id
    key = RetentionKey("checkpoint", str(head))
    source, _ = await versions(stopped, tmp_path)
    first_release = (await clocks(stopped))[key]
    assert first_release is not None
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    generation = await library.generation()
    ready = asyncio.Event()
    writer_pid: list[int] = []
    task: asyncio.Task[None] | None = None

    async def release_again() -> None:
        """
        在另一连接中按正确的提交代数解除账户 pin。
        """
        async with stopped.database.begin() as session:
            writer_pid.append(int(await session.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            await library.service(session).execute(
                stopped.owner,
                SkillRuleRequest(
                    command="unpin",
                    skill="learning",
                    scope=SkillScope(account_id=stopped.account),
                    expected_generation=generation + int(commit_first),
                    idempotency_key=str(uuid4()),
                ),
            )

    try:
        async with stopped.database() as session:
            await library.service(session).execute(
                stopped.owner,
                SkillRuleRequest(
                    command="pin",
                    skill="learning",
                    revision=str(source),
                    scope=SkillScope(account_id=stopped.account),
                    expected_generation=generation,
                    idempotency_key=str(uuid4()),
                ),
            )
            task = asyncio.create_task(release_again())
            await asyncio.wait_for(ready.wait(), timeout=2)
            blocked = False
            async with stopped.database() as observer:
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(writer_pid[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
            assert blocked and not task.done()
            if commit_first:
                await session.commit()
            else:
                await session.rollback()
    finally:
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
    last_release = (await clocks(stopped))[key]
    assert last_release is not None
    assert (last_release > first_release) if commit_first else (last_release == first_release)
