"""
验证快照事务的规则生效边界、原子引用、重试与严格准备任务绑定。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness, runtime
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_library import LibraryHarness

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
)
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.snapshots import SkillSnapshotService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.fixture
async def prepared(database: async_sessionmaker[AsyncSession], tmp_path: Path) -> RuntimeHarness:
    """
    准备已接管的空账户目录，后续仅由服务初始化有效分支。

    :param database (async_sessionmaker[AsyncSession]): 数据库工厂
    :param tmp_path (Path): 内容卷
    :return RuntimeHarness: 待预约的真实会话身份
    """
    return await prepare_runtime(database, tmp_path)


async def prepare_runtime(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> RuntimeHarness:
    """
    复用空账户接管夹具，允许真实恢复验收为源卷选择独立目录。

    :param database (async_sessionmaker[AsyncSession]): 数据库事务工厂
    :param tmp_path (Path): 当前隔离内容卷
    :return RuntimeHarness: 尚未预约的账户与会话身份
    """
    state = await runtime(database, tmp_path)
    async with database.begin() as session:
        await session.execute(
            delete(SessionSkillSnapshot).where(SessionSkillSnapshot.id == state.snapshot)
        )
        await session.execute(
            delete(SkillDirectoryMember).where(
                SkillDirectoryMember.directory_checkpoint_id == state.directory
            )
        )
        await session.execute(delete(SkillCheckpoint).where(SkillCheckpoint.id == state.item))
        await session.execute(
            update(AccountSkillDirectoryState)
            .where(AccountSkillDirectoryState.account_id == state.account)
            .values(mode="managed_v1", head_checkpoint_id=state.directory)
        )
        await session.execute(
            update(NodeTask)
            .where(NodeTask.id == state.task)
            .values(
                task_type="create_tool_session",
                payload={
                    "session_id": str(state.session),
                    "user_id": str(state.owner),
                    "tool_account_id": str(state.account),
                    "runtime_backend": "native",
                },
            )
        )
    return state


async def reserve(
    state: RuntimeHarness, root: Path, task: UUID | None = None
) -> SessionSkillSnapshot:
    """
    用独立请求事务执行真实服务预约。

    :param state (RuntimeHarness): 准备身份
    :param root (Path): 私有内容卷
    :param task (UUID | None): 可选替换任务
    :return SessionSkillSnapshot: 已提交精确快照
    """
    async with state.database.begin() as session:
        service = SkillSnapshotService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        return await service.reserve(
            state.owner, state.session, task or state.task, {"ego-browser": "test-release"}
        )


async def test_reservation_pins_rules_and_restart_reuses_original(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    提交后停用不会改写重试快照，原始物化项和分支仍受引用保护。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    first = await reserve(prepared, tmp_path)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable", skill="learning", idempotency_key=str(uuid4()), expected_generation=1
        )
    )
    second = await reserve(prepared, tmp_path)
    assert first.id == second.id and second.library_generation == 1
    async with prepared.database() as session:
        items = (
            await session.scalars(
                select(SessionSkillSnapshotItem).where(
                    SessionSkillSnapshotItem.snapshot_id == first.id
                )
            )
        ).all()
        assert len(items) == 1 and items[0].entry_name == "learning"
        branch = await session.get(AccountSkillState, prepared.state)
        assert branch is not None and branch.head_checkpoint_id == items[0].checkpoint_id
        assert items[0].state_epoch == 1 and items[0].resolution_json["enabled"] is True


async def test_change_before_reservation_is_visible(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    预约之前提交的停用必须影响新会话，不把空物化目录写回账户 head。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable", skill="learning", idempotency_key=str(uuid4()), expected_generation=1
        )
    )
    snapshot = await reserve(prepared, tmp_path)
    assert snapshot.library_generation == 2 and snapshot.tree_digest == prepared.tree
    async with prepared.database() as session:
        items = (
            await session.scalars(
                select(SessionSkillSnapshotItem).where(
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id
                )
            )
        ).all()
        assert not items
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None and directory.head_checkpoint_id == prepared.directory


@pytest.mark.parametrize("field", ["session_id", "user_id", "tool_account_id", "runtime_backend"])
async def test_task_payload_cannot_substitute_binding(
    prepared: RuntimeHarness, tmp_path: Path, field: str
) -> None:
    """
    同一节点任务也必须匹配精确身份字段。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    :param field (str): 待篡改字段
    """
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.payload = {**task.payload, field: str(uuid4())}
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path)
    assert error.value.code == "SNAPSHOT_BINDING_MISMATCH"


async def test_retry_cannot_switch_task(prepared: RuntimeHarness, tmp_path: Path) -> None:
    """
    已固定快照的重试不能借相同会话身份取得另一个任务授权。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    await reserve(prepared, tmp_path)
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path, uuid4())
    assert error.value.code == "SNAPSHOT_BINDING_MISMATCH"


async def test_failed_outer_transaction_releases_every_new_reference(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    快照创建之后外层失败，分支 head 和完整快照引用必须一起回滚。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    with pytest.raises(RuntimeError, match="rollback"):
        async with prepared.database.begin() as session:
            service = SkillSnapshotService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            )
            await service.reserve(prepared.owner, prepared.session, prepared.task, {})
            raise RuntimeError("rollback")
    async with prepared.database() as session:
        assert (
            await session.scalar(
                select(SessionSkillSnapshot).where(SessionSkillSnapshot.user_id == prepared.owner)
            )
            is None
        )
        branch = await session.get(AccountSkillState, prepared.state)
        assert branch is not None and branch.head_checkpoint_id is None
    assert (await reserve(prepared, tmp_path)).library_generation == 1


@pytest.mark.parametrize("reason", ["legacy", "expired"])
async def test_reservation_never_takes_over_or_resets_implicitly(
    prepared: RuntimeHarness, tmp_path: Path, reason: str
) -> None:
    """
    缺少接管或过期分支必须交由显式流程处理，不能静默恢复原始包。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    :param reason (str): 阻塞条件
    """
    async with prepared.database.begin() as session:
        if reason == "legacy":
            await session.execute(
                update(AccountSkillDirectoryState)
                .where(AccountSkillDirectoryState.account_id == prepared.account)
                .values(mode="legacy")
            )
        else:
            await session.execute(
                update(AccountSkillState)
                .where(AccountSkillState.id == prepared.state)
                .values(expired=True)
            )
    with pytest.raises(SkillContentError) as error:
        await reserve(prepared, tmp_path)
    assert error.value.code == ("MIGRATION_PENDING" if reason == "legacy" else "STATE_EXPIRED")


async def test_concurrent_reservations_share_one_snapshot(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两个真实数据库连接竞争同一会话只能得到一个精确快照。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    import asyncio

    first, second = await asyncio.gather(reserve(prepared, tmp_path), reserve(prepared, tmp_path))
    assert first.id == second.id and first.tree_digest == second.tree_digest
    async with prepared.database() as session:
        snapshots = (
            await session.scalars(
                select(SessionSkillSnapshot).where(SessionSkillSnapshot.user_id == prepared.owner)
            )
        ).all()
        assert len(snapshots) == 1


async def test_new_revision_requires_migration_before_initialization(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已有运行分支时切新版本不能静默初始化原始包并丢弃旧版学习数据。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    from agent_remote_server.schemas.skill_library import SkillUpdateRequest

    first = await reserve(prepared, tmp_path)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    candidate = await library.candidate(version="two")
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=candidate,
            idempotency_key=str(uuid4()),
            expected_generation=1,
        )
    )
    from skill_publication_support import new_session

    with pytest.raises(SkillContentError) as error:
        await new_session(prepared, tmp_path)
    assert error.value.code == "STATE_MIGRATION_REQUIRED"
    async with prepared.database() as session:
        assert await session.get(SessionSkillSnapshot, first.id) is not None


async def test_reservation_refreshes_entities_loaded_before_user_lock(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同一请求早先缓存的 ORM 条目不能与锁内读取的新库代数混用。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    """
    from agent_remote_server.models.skill_library import SkillInstallation

    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    async with prepared.database.begin() as session:
        stale = await session.scalar(
            select(SkillInstallation).where(SkillInstallation.user_id == prepared.owner)
        )
        assert stale is not None and stale.default_enabled
        await library.execute(
            SkillRuleRequest(
                command="disable",
                skill="learning",
                idempotency_key=str(uuid4()),
                expected_generation=1,
            )
        )
        snapshot = await SkillSnapshotService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).reserve(prepared.owner, prepared.session, prepared.task, {})
        assert snapshot.library_generation == 2
        items = (
            await session.scalars(
                select(SessionSkillSnapshotItem).where(
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id
                )
            )
        ).all()
        assert not items


@pytest.mark.parametrize("scope_kind", ["tool", "account"])
async def test_reservation_refreshes_cached_override_fields(
    prepared: RuntimeHarness, tmp_path: Path, scope_kind: str
) -> None:
    """
    锁内必须刷新已加载的工具或账户覆盖，不能只刷新用户默认字段。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    :param scope_kind (str): 待缓存的覆盖层级
    """
    from agent_remote_server.models.skill_library import SkillAccountOverride, SkillToolOverride
    from agent_remote_server.schemas.skill_library import SkillScope

    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    scope = (
        SkillScope(account_id=prepared.account)
        if scope_kind == "account"
        else SkillScope(tools=("claude",))
    )
    await library.execute(
        SkillRuleRequest(
            command="enable",
            skill="learning",
            scope=scope,
            idempotency_key=str(uuid4()),
            expected_generation=1,
        )
    )
    async with prepared.database.begin() as session:
        if scope_kind == "account":
            stale = await session.scalar(
                select(SkillAccountOverride).where(SkillAccountOverride.user_id == prepared.owner)
            )
        else:
            stale = await session.scalar(
                select(SkillToolOverride).where(SkillToolOverride.user_id == prepared.owner)
            )
        assert stale is not None and stale.enabled
        await library.execute(
            SkillRuleRequest(
                command="disable",
                skill="learning",
                scope=scope,
                idempotency_key=str(uuid4()),
                expected_generation=2,
            )
        )
        snapshot = await SkillSnapshotService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).reserve(prepared.owner, prepared.session, prepared.task, {})
        assert snapshot.library_generation == 3
        items = (
            await session.scalars(
                select(SessionSkillSnapshotItem).where(
                    SessionSkillSnapshotItem.snapshot_id == snapshot.id
                )
            )
        ).all()
        assert not items
