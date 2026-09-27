"""
验证既有删除和旧会话入口不会绕过运行状态保留。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness, runtime
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import Session, ToolAccount, User
from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.services.sessions import ToolSessionService
from agent_remote_server.services.skills.account_lifecycle import forget_account_overrides
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.session_admission import SkillSessionAdmission


@pytest.fixture
async def state(database: async_sessionmaker[AsyncSession], tmp_path: Path) -> RuntimeHarness:
    """
    建立有待上传快照的已停止会话。

    :param database (async_sessionmaker[AsyncSession]): 数据库工厂
    :param tmp_path (Path): 内容卷
    :return RuntimeHarness: 真实待保留身份
    """
    result = await runtime(database, tmp_path)
    async with database.begin() as session:
        await session.execute(
            update(Session).where(Session.id == result.session).values(status="stopped")
        )
    return result


@pytest.mark.parametrize("bulk", [False, True])
async def test_pending_state_blocks_single_and_bulk_delete(
    state: RuntimeHarness, bulk: bool
) -> None:
    """
    进程已停止也不能删除尚未持久化内容的展示与定位记录。

    :param state (RuntimeHarness): 待保留快照
    :param bulk (bool): 是否使用批量删除入口
    """
    async with state.database() as session:
        user = await session.get(User, state.owner)
        assert user is not None
        service = ToolSessionService(session, Settings(secret_key="test-lifecycle"))
        with pytest.raises(SkillContentError) as error:
            if bulk:
                await service.delete_inactive_sessions(user=user)
            else:
                await service.delete_session(user=user, session_id=state.session)
        assert error.value.code == "STATE_PENDING"
    async with state.database() as session:
        assert await session.get(Session, state.session) is not None
        snapshot = await session.get(SessionSkillSnapshot, state.snapshot)
        assert snapshot is not None and snapshot.session_id == state.session


@pytest.mark.parametrize("bulk", [False, True])
async def test_durable_finalization_survives_session_deletion(
    state: RuntimeHarness, bulk: bool
) -> None:
    """
    保留最终完整提交后允许删除展示，快照审计身份和内容仍可恢复。

    :param state (RuntimeHarness): 保留快照
    :param bulk (bool): 是否批量删除
    """
    async with state.database.begin() as session:
        await session.execute(
            update(SessionSkillSnapshot)
            .where(SessionSkillSnapshot.id == state.snapshot)
            .values(status="retained")
        )
        session.add(
            SkillFinalization(
                user_id=state.owner,
                account_id=state.account,
                node_id=state.node,
                snapshot_id=state.snapshot,
                idempotency_key=str(uuid4()),
                request_digest="a" * 64,
                incoming_digest=state.tree,
                tree_digest=state.tree,
                checkpoint_id=state.directory,
                unclean=False,
                status="conflicted",
            )
        )
    async with state.database() as session:
        user = await session.get(User, state.owner)
        assert user is not None
        service = ToolSessionService(session, Settings(secret_key="test-lifecycle"))
        if bulk:
            assert await service.delete_inactive_sessions(user=user) == 1
        else:
            await service.delete_session(user=user, session_id=state.session)
    async with state.database() as session:
        assert await session.get(Session, state.session) is None
        snapshot = await session.get(SessionSkillSnapshot, state.snapshot)
        assert snapshot is not None and snapshot.session_id is None
        assert snapshot.session_reference_id == state.session
        finalization = await session.scalar(
            select(SkillFinalization).where(SkillFinalization.snapshot_id == state.snapshot)
        )
        assert finalization is not None and finalization.tree_digest == state.tree


async def test_retained_flag_without_durable_finalization_cannot_release_session(
    state: RuntimeHarness,
) -> None:
    """
    单独改变快照状态不能冒充内容已保存的证据。

    :param state (RuntimeHarness): 待保留快照
    """
    async with state.database.begin() as session:
        await session.execute(
            update(SessionSkillSnapshot)
            .where(SessionSkillSnapshot.id == state.snapshot)
            .values(status="retained")
        )
    async with state.database() as session:
        user = await session.get(User, state.owner)
        assert user is not None
        with pytest.raises(SkillContentError, match="state"):
            await ToolSessionService(session, Settings(secret_key="test-lifecycle")).delete_session(
                user=user, session_id=state.session
            )


async def test_runtime_root_blocks_account_rule_removal(state: RuntimeHarness) -> None:
    """
    已存在账户运行根时，账户删除流程不能先删除规则而失去归属。

    :param state (RuntimeHarness): 运行态根
    """
    with pytest.raises(SkillContentError) as error:
        async with state.database.begin() as session:
            await forget_account_overrides(session, state.owner, state.account)
    assert error.value.code == "STATE_RETAINED"


async def test_empty_managed_library_cannot_fall_back_to_legacy(state: RuntimeHarness) -> None:
    """
    全部禁用不改变目录权威，也不能忽略根级状态退回旧共享目录。

    :param state (RuntimeHarness): 运行态根
    """
    async with state.database.begin() as session:
        await session.execute(
            update(SkillInstallation)
            .where(SkillInstallation.user_id == state.owner)
            .values(default_enabled=False)
        )
        await session.execute(
            update(AccountSkillDirectoryState)
            .where(AccountSkillDirectoryState.account_id == state.account)
            .values(mode="managed_v1", head_checkpoint_id=state.directory)
        )
    async with state.database() as session:
        account = await session.get(ToolAccount, state.account)
        assert account is not None
        enabled = Settings(secret_key="test-lifecycle", skill_manager_enabled=True)
        assert await SkillSessionAdmission(session, enabled).required(account)
        with pytest.raises(ApiError) as error:
            await SkillSessionAdmission(session, Settings(secret_key="test-lifecycle")).required(
                account
            )
        assert error.value.code == "SKILL_MANAGER_DISABLED"
