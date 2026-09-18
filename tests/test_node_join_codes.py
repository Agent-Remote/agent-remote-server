"""
验证节点加入码行为。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_remote_server.config import Settings
from agent_remote_server.db import Base
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, Node, NodeJoinCode, User
from agent_remote_server.security import hash_token
from agent_remote_server.services.nodes import NodeService


@pytest.fixture
async def join_context(
    tmp_path: Path,
) -> AsyncIterator[tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]]:
    """
    创建使用文件 SQLite 的加入码测试上下文。

    :param tmp_path (Path): pytest 临时目录
    :return AsyncIterator[tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]]: 拼接上下文
    """

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'join-codes.sqlite3'}"
    settings = Settings(
        secret_key="join-code-test-secret",
        database_url=database_url,
        public_base_url="https://control.example.test",
        log_level="CRITICAL",
    )
    engine: AsyncEngine = create_async_engine(database_url, connect_args={"timeout": 10})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user = User(
            username="join-code-admin",
            display_name="Join Code Admin",
            role="admin",
            status="active",
            password_hash="test",
            totp_enabled=False,
        )
        node = Node(
            name="join-code-node",
            status="offline",
            region_code="US",
            tags=[],
            weight=1,
            supported_tool_types=["claude"],
            allowed_runtime_backends=["native"],
            default_runtime_backend="native",
            runtime_policy={},
            runtime_capabilities={},
        )
        session.add_all([user, node])
        await session.flush()
        user_id = user.id
        node_id = node.id
        await session.commit()
    try:
        yield settings, session_factory, user_id, node_id
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _issue_code(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    node_id: UUID,
    *,
    enabled: bool | None = None,
    exchange_id: str | None = None,
) -> str:
    """
    签发代码。

    :param settings (Settings): 配置
    :param session_factory (async_sessionmaker[AsyncSession]): 会话 factory
    :param user_id (UUID): 用户 ID
    :param node_id (UUID): 节点 ID
    :param enabled (bool | None): 启用状态
    :param exchange_id (str | None): 交换 ID
    :return str: 代码
    """
    async with session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        result = await NodeService(session, settings).issue_join_code(
            actor=user,
            node_id=node_id,
            ego_browser_enabled=enabled,
            exchange_id=exchange_id,
        )
        return result.raw_code


async def _exchange(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    node_id: UUID,
    join_code: str | None,
    exchange_id: str,
    *,
    ego_browser_enabled: bool | None = None,
) -> str:
    """
    返回交换。

    :param settings (Settings): 配置
    :param session_factory (async_sessionmaker[AsyncSession]): 会话 factory
    :param node_id (UUID): 节点 ID
    :param join_code (str | None): 加入码
    :param exchange_id (str): 交换 ID
    :param ego_browser_enabled (bool | None): Ego Browser 启用状态
    :return str: 交换
    """
    async with session_factory() as session:
        result = await NodeService(session, settings).exchange_join_code(
            node_id=node_id,
            version="0.2.16",
            join_code=join_code,
            exchange_id=exchange_id,
            wrapper_version=settings.ego_browser_expected_wrapper_version,
            skill_version=settings.ego_browser_expected_skill_version,
            runtime_version="0.2.16",
            artifact_digest=f"sha256:{settings.ego_browser_expected_skill_tree_sha256}",
            ego_browser_enabled=ego_browser_enabled,
        )
        return result.raw_node_token


@pytest.mark.asyncio
async def test_join_code_exchange_recovers_the_same_token(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    首次交换、带 code 重试和无 code 恢复都返回同一凭据。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    exchange_id = "exchange-recovery-0001"

    first_token = await _exchange(settings, session_factory, node_id, code, exchange_id)
    recovered_token = await _exchange(settings, session_factory, node_id, None, exchange_id)
    replayed_token = await _exchange(settings, session_factory, node_id, code, exchange_id)

    assert first_token == recovered_token == replayed_token


@pytest.mark.asyncio
async def test_expired_join_code_is_rejected_without_consumption(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    过期加入码不得占用 exchange ID 或签发 Node 凭据。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(
                NodeJoinCode.code_hash == hash_token(settings.secret_key, code)
            )
        )
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    with pytest.raises(ApiError) as caught:
        await _exchange(
            settings,
            session_factory,
            node_id,
            code,
            "exchange-expired-code-01",
        )
    assert caught.value.code == "NODE_JOIN_CODE_EXPIRED"

    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(
                NodeJoinCode.code_hash == hash_token(settings.secret_key, code)
            )
        )
        assert record is not None
        assert record.consumed_at is None
        assert record.exchange_id is None
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.node_token_hash is None


@pytest.mark.asyncio
async def test_consumed_join_code_rejects_replay_with_a_different_exchange(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    已消费加入码只能恢复原 exchange，不得为新 exchange 签发凭据。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    original_exchange_id = "exchange-consumed-original-01"
    original_token = await _exchange(
        settings,
        session_factory,
        node_id,
        code,
        original_exchange_id,
    )

    with pytest.raises(ApiError) as caught:
        await _exchange(
            settings,
            session_factory,
            node_id,
            code,
            "exchange-consumed-replay-01",
        )
    assert caught.value.code == "NODE_JOIN_CODE_REPLAYED"

    async with session_factory() as session:
        records = list(await session.scalars(select(NodeJoinCode)))
        assert len(records) == 1
        assert records[0].exchange_id == original_exchange_id
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.node_token_hash == hash_token(settings.secret_key, original_token)


@pytest.mark.asyncio
async def test_managed_join_code_prebinds_exchange_and_rejects_duplicate_issuance(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    受管 CLI 的交换 ID 在 code 离开 Server 前持久化并保持唯一。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    exchange_id = "managed-exchange-prebound-0001"
    code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        exchange_id=exchange_id,
    )
    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(NodeJoinCode.exchange_id == exchange_id)
        )
        assert record is not None
        assert record.consumed_at is None

    with pytest.raises(ApiError) as caught:
        await _issue_code(
            settings,
            session_factory,
            user_id,
            node_id,
            exchange_id=exchange_id,
        )
    assert caught.value.code == "NODE_JOIN_CODE_EXCHANGE_CONFLICT"

    token = await _exchange(settings, session_factory, node_id, code, exchange_id)
    assert token.startswith("node_")


@pytest.mark.asyncio
async def test_targeted_join_code_revoke_reports_consumption_and_preserves_other_codes(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    精确撤销不影响并行 code，并向 CLI 区分已消费交换。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    revoked_exchange = "targeted-revoke-exchange-0001"
    consumed_exchange = "targeted-consumed-exchange-01"
    revoked_code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        exchange_id=revoked_exchange,
    )
    consumed_code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        exchange_id=consumed_exchange,
    )

    async with session_factory() as session:
        actor = await session.get(User, user_id)
        assert actor is not None
        service = NodeService(session, settings)
        assert (
            await service.revoke_join_codes(
                actor=actor, node_id=node_id, exchange_id=revoked_exchange
            )
            == "revoked"
        )

    with pytest.raises(ApiError) as caught:
        await _exchange(
            settings,
            session_factory,
            node_id,
            revoked_code,
            revoked_exchange,
        )
    assert caught.value.code == "NODE_JOIN_CODE_REVOKED"
    node_token = await _exchange(
        settings,
        session_factory,
        node_id,
        consumed_code,
        consumed_exchange,
    )

    async with session_factory() as session:
        actor = await session.get(User, user_id)
        assert actor is not None
        service = NodeService(session, settings)
        assert (
            await service.revoke_join_codes(
                actor=actor, node_id=node_id, exchange_id=consumed_exchange
            )
            == "consumed"
        )
        assert (
            await service.revoke_join_codes(
                actor=actor,
                node_id=node_id,
                exchange_id="missing-exchange-00000001",
            )
            == "missing"
        )
        audits = list(await session.scalars(select(AuditLog)))
        rendered = json.dumps([audit.details for audit in audits])
        assert revoked_code not in rendered
        assert consumed_code not in rendered
        assert node_token not in rendered


@pytest.mark.asyncio
async def test_join_code_issue_and_revoke_require_an_administrator(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    普通用户不能签发或撤销 Node 加入码。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, _, node_id = join_context
    async with session_factory() as session:
        user = User(
            username="join-code-user",
            display_name="Join Code User",
            role="user",
            status="active",
            password_hash="test",
            totp_enabled=False,
        )
        session.add(user)
        await session.commit()
        service = NodeService(session, settings)
        with pytest.raises(ApiError) as issue_error:
            await service.issue_join_code(actor=user, node_id=node_id)
        assert issue_error.value.code == "COMMON_FORBIDDEN"
        with pytest.raises(ApiError) as revoke_error:
            await service.revoke_join_codes(actor=user, node_id=node_id)
        assert revoke_error.value.code == "COMMON_FORBIDDEN"


@pytest.mark.asyncio
async def test_join_code_exchange_rejects_a_second_code_for_same_exchange(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    不同加入码不能借用已占用的 exchange ID。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    first_code = await _issue_code(settings, session_factory, user_id, node_id)
    second_code = await _issue_code(settings, session_factory, user_id, node_id)
    exchange_id = "exchange-conflict-01"

    first_token = await _exchange(settings, session_factory, node_id, first_code, exchange_id)
    with pytest.raises(ApiError) as caught:
        await _exchange(settings, session_factory, node_id, second_code, exchange_id)

    assert caught.value.code == "NODE_JOIN_CODE_EXCHANGE_CONFLICT"
    async with session_factory() as session:
        rows = list(await session.scalars(select(NodeJoinCode)))
        assert len(rows) == 2
        second_hash = hash_token(settings.secret_key, second_code)
        second_record = next(row for row in rows if row.code_hash == second_hash)
        assert second_record.consumed_at is None
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.node_token_hash == hash_token(settings.secret_key, first_token)


@pytest.mark.asyncio
async def test_concurrent_join_code_exchange_has_one_winner(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    并发消费不同加入码时唯一 exchange 只允许一个 token 胜出。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    first_code = await _issue_code(settings, session_factory, user_id, node_id)
    second_code = await _issue_code(settings, session_factory, user_id, node_id)
    exchange_id = "exchange-concurrent-1"

    results = await asyncio.gather(
        _exchange(settings, session_factory, node_id, first_code, exchange_id),
        _exchange(settings, session_factory, node_id, second_code, exchange_id),
        return_exceptions=True,
    )

    tokens = [result for result in results if isinstance(result, str)]
    errors = [result for result in results if isinstance(result, ApiError)]
    assert len(tokens) == 1
    assert len(errors) == 1
    assert errors[0].code == "NODE_JOIN_CODE_EXCHANGE_CONFLICT"
    async with session_factory() as session:
        rows = list(await session.scalars(select(NodeJoinCode)))
        assert sum(row.consumed_at is not None for row in rows) == 1
        assert sum(row.exchange_id == exchange_id for row in rows) == 1


@pytest.mark.asyncio
async def test_join_code_exchange_fails_closed_for_corrupt_cached_result(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    已消费记录的密文损坏时不得重新签发或返回空凭据。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    exchange_id = "exchange-corrupt-01"
    await _exchange(settings, session_factory, node_id, code, exchange_id)

    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(NodeJoinCode.exchange_id == exchange_id)
        )
        assert record is not None
        record.encrypted_node_token = b"corrupt"
        await session.commit()

    with pytest.raises(ApiError) as caught:
        await _exchange(settings, session_factory, node_id, None, exchange_id)
    assert caught.value.code == "UNKNOWN_RESULT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code_intent", "client_intent", "expected_enabled"),
    [
        (None, None, False),
        (True, True, True),
        (False, False, False),
    ],
)
async def test_join_code_intent_and_profile_digest_are_authoritative(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
    code_intent: bool | None,
    client_intent: bool | None,
    expected_enabled: bool,
) -> None:
    """
    加入意图必须在首次登记生效，且 profile 摘要覆盖 preserve/true/false。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    :param code_intent (bool | None): 代码 intent
    :param client_intent (bool | None): 客户端 intent
    :param expected_enabled (bool): 预期启用状态
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        enabled=code_intent,
    )
    exchange_id = f"exchange-intent-{str(code_intent).lower()}-01"
    await _exchange(
        settings,
        session_factory,
        node_id,
        code,
        exchange_id,
        ego_browser_enabled=client_intent,
    )

    async with session_factory() as session:
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.ego_browser_enabled is expected_enabled
        record = await session.scalar(
            select(NodeJoinCode).where(NodeJoinCode.exchange_id == exchange_id)
        )
        assert record is not None
        assert record.ego_browser_enabled is code_intent
        assert record.profile_digest == NodeService._join_profile_digest(  # noqa: SLF001
            server_origin=record.server_origin,
            node_id=node_id,
            release_profile=record.release_profile or "",
            wrapper_version=record.wrapper_version or "",
            skill_version=record.skill_version or "",
            runtime_version=record.runtime_version,
            artifact_digest=record.artifact_digest or "",
            ego_browser_enabled=record.ego_browser_enabled,
        )


@pytest.mark.asyncio
async def test_existing_node_preserves_omitted_join_intent(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    已有 Node 的普通重装省略意图时必须保留既有管理员配置。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    initial_code = await _issue_code(settings, session_factory, user_id, node_id)
    await _exchange(
        settings,
        session_factory,
        node_id,
        initial_code,
        "exchange-initial-preserve-01",
    )
    enabled_code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        enabled=True,
    )
    await _exchange(
        settings,
        session_factory,
        node_id,
        enabled_code,
        "exchange-upgrade-preserve-01",
    )

    async with session_factory() as session:
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.ego_browser_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_enabled", "code_intent", "client_intent", "expected_enabled"),
    [
        (False, True, True, True),
        (True, False, False, False),
    ],
)
async def test_existing_node_changes_join_intent_only_when_explicitly_echoed(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
    initial_enabled: bool,
    code_intent: bool,
    client_intent: bool,
    expected_enabled: bool,
) -> None:
    """
    已有 Node 只有在客户端明确回显授权意图时才改变配置。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    :param initial_enabled (bool): 初始状态启用状态
    :param code_intent (bool): 代码 intent
    :param client_intent (bool): 客户端 intent
    :param expected_enabled (bool): 预期启用状态
    """

    settings, session_factory, user_id, node_id = join_context
    initial_code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        enabled=initial_enabled,
    )
    await _exchange(
        settings,
        session_factory,
        node_id,
        initial_code,
        "exchange-initial-explicit-01",
        ego_browser_enabled=initial_enabled,
    )
    code = await _issue_code(
        settings,
        session_factory,
        user_id,
        node_id,
        enabled=code_intent,
    )
    await _exchange(
        settings,
        session_factory,
        node_id,
        code,
        f"exchange-change-{str(code_intent).lower()}-01",
        ego_browser_enabled=client_intent,
    )

    async with session_factory() as session:
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.ego_browser_enabled is expected_enabled


@pytest.mark.asyncio
async def test_explicit_intent_against_unspecified_code_is_rejected_before_consumption(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    未携带意图的加入码不得被客户端显式升级或降级。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    exchange_id = "exchange-intent-conflict-01"
    with pytest.raises(ApiError) as caught:
        await _exchange(
            settings,
            session_factory,
            node_id,
            code,
            exchange_id,
            ego_browser_enabled=True,
        )
    assert caught.value.code == "NODE_JOIN_CODE_PROFILE_MISMATCH"

    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(
                NodeJoinCode.code_hash == hash_token(settings.secret_key, code)
            )
        )
        assert record is not None
        assert record.consumed_at is None
        assert record.exchange_id is None
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.node_token_hash is None


@pytest.mark.asyncio
async def test_join_code_is_rejected_after_server_release_profile_changes(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    旧加入码不能在 Server 晋级新 wrapper/Skill 后继续授权。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    settings.ego_browser_expected_wrapper_version = "0.1.12"

    with pytest.raises(ApiError) as caught:
        await _exchange(settings, session_factory, node_id, code, "exchange-stale-profile-01")
    assert caught.value.code == "NODE_JOIN_CODE_PROFILE_MISMATCH"

    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(
                NodeJoinCode.code_hash == hash_token(settings.secret_key, code)
            )
        )
        assert record is not None
        assert record.consumed_at is None
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.node_token_hash is None


@pytest.mark.asyncio
async def test_join_code_profile_metadata_tampering_fails_closed_before_consumption(
    join_context: tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID],
) -> None:
    """
    加入码记录的 profile/origin 被篡改时不得消费或签发 Node token。

    :param join_context (tuple[Settings, async_sessionmaker[AsyncSession], UUID, UUID]): 拼接上下文
    """

    settings, session_factory, user_id, node_id = join_context
    code = await _issue_code(settings, session_factory, user_id, node_id)
    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(
                NodeJoinCode.code_hash == hash_token(settings.secret_key, code)
            )
        )
        assert record is not None
        record.server_origin = "https://other.example.test"
        await session.commit()

    with pytest.raises(ApiError) as caught:
        await _exchange(settings, session_factory, node_id, code, "exchange-origin-tamper-01")
    assert caught.value.code == "NODE_JOIN_CODE_PROFILE_MISMATCH"

    async with session_factory() as session:
        record = await session.scalar(
            select(NodeJoinCode).where(
                NodeJoinCode.code_hash == hash_token(settings.secret_key, code)
            )
        )
        assert record is not None
        assert record.consumed_at is None
        node = await session.get(Node, node_id)
        assert node is not None
        assert node.node_token_hash is None
