"""
验证账户诊断与原会话元数据的身份隔离、只读性和同步时间证据。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import delete
from test_skill_compaction_reclamation import persisted_content
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_local import activate, register, source
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve

from agent_remote_server.models import Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.effective_queries import SkillEffectiveQueryService
from agent_remote_server.services.skills.session_retention import release_retained_session_reference


async def test_original_session_survives_configuration_and_epoch_changes(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    当前规则和分支纪元变化不修改历史固定身份，删除会话后原所有者仍可查。

    :param user_client (AsyncClient): 用户认证客户端
    :param stopped (RuntimeHarness): 原始固定会话
    :param tmp_path (Path): 私有内容卷
    """
    finalization = await ingest(stopped, tmp_path, {})
    await publish(stopped, tmp_path, finalization)
    path = f"/api/v1/skills/sessions/{stopped.session}"
    response = await user_client.get(path)
    assert response.status_code == 200, response.text
    original = response.json()["data"]
    assert original["basis"] == "session_snapshot"
    assert original["system_items"][0]["release"] == {"legacy_reference": "test-release"}
    assert original["items"][0]["resolution"]["included"] is True
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable", skill="learning", idempotency_key=str(uuid4()), expected_generation=1
        )
    )
    async with stopped.database.begin() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        branch.epoch += 1
        live = await session.get(Session, stopped.session)
        assert live is not None
        await release_retained_session_reference(session, live)
        await session.execute(delete(Session).where(Session.id == stopped.session))
    before = await persisted_content(stopped)
    again = await user_client.get(path)
    assert again.status_code == 200, again.text
    assert again.json()["data"] == original
    assert await persisted_content(stopped) == before
    foreign = await user(stopped.database)
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await SkillEffectiveQueryService(session).session(foreign, stopped.session)
        assert error.value.code == "SESSION_NOT_FOUND"


async def test_snapshot_pagination_preserves_library_and_local_selection(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    有界页面保留本地和库来源，游标必须来自原快照且查询不初始化新状态。

    :param prepared (RuntimeHarness): 未预约账户
    :param tmp_path (Path): 私有内容卷
    """
    local = await register(prepared, tmp_path, await source(prepared, tmp_path))
    await activate(prepared, local)
    await reserve(prepared, tmp_path)
    async with prepared.database.begin() as session:
        query = SkillEffectiveQueryService(session)
        first = await query.session(prepared.owner, prepared.session, 1)
        assert first.next_cursor == "learning"
        second = await query.session(prepared.owner, prepared.session, 1, first.next_cursor)
        assert second.next_cursor is None
        assert second.items[0].skill_id == local.id and second.items[0].origin == "account_local"
        assert second.items[0].resolution.revision_id == local.default_revision_id
        assert first.snapshot_id == second.snapshot_id
        assert not session.new and not session.dirty and not session.deleted
        with pytest.raises(SkillContentError) as error:
            await query.session(prepared.owner, prepared.session, 1, "invented")
        assert error.value.code == "INVALID_REQUEST"


async def test_legacy_session_is_unknown_instead_of_reconstructed(prepared: RuntimeHarness) -> None:
    """
    有所有权的无快照会话与不存在的会话有不同结果，不推断当前系统发布。

    :param prepared (RuntimeHarness): 未预约旧会话
    """
    async with prepared.database.begin() as session:
        query = SkillEffectiveQueryService(session)
        result = await query.session(prepared.owner, prepared.session)
        assert result.basis == "legacy_unrecorded" and not result.items and not result.system_items
        assert result.snapshot_id is None and result.content_retained is None
        with pytest.raises(SkillContentError) as error:
            await query.session(prepared.owner, uuid4())
        assert error.value.code == "SESSION_NOT_FOUND"
        with pytest.raises(SkillContentError):
            await query.session(prepared.owner, prepared.session, cursor="learning")


async def test_account_queries_follow_exact_selected_revision_without_preparation(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    空分支、初始化、切版与过期状态各自解释，不借用旧版头或触发迁移。

    :param prepared (RuntimeHarness): 未初始化账户
    :param tmp_path (Path): 私有内容卷
    """
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    scope = SkillScope(account_id=prepared.account)
    async with prepared.database.begin() as session:
        item = await library.service(session).info(prepared.owner, "learning", scope=scope)
        view = await SkillEffectiveQueryService(session).account(
            prepared.owner, prepared.account, item
        )
        assert view.preparation == "uninitialized" and view.checkpoint_id is None
    await reserve(prepared, tmp_path)
    async with prepared.database.begin() as session:
        item = await library.service(session).info(prepared.owner, "learning", scope=scope)
        view = await SkillEffectiveQueryService(session).account(
            prepared.owner, prepared.account, item
        )
        assert view.preparation == "initialized" and view.checkpoint_id is not None
        branch = await session.get(AccountSkillState, prepared.state)
        assert branch is not None
        branch.expired = True
    async with prepared.database.begin() as session:
        item = await library.service(session).info(prepared.owner, "learning", scope=scope)
        view = await SkillEffectiveQueryService(session).account(
            prepared.owner, prepared.account, item
        )
        assert view.preparation == "state_expired"
    candidate = await library.candidate(version="two")
    await library.execute(
        SkillUpdateRequest(
            skill="learning", item=candidate, idempotency_key=str(uuid4()), expected_generation=1
        )
    )
    async with prepared.database.begin() as session:
        item = await library.service(session).info(prepared.owner, "learning", scope=scope)
        view = await SkillEffectiveQueryService(session).account(
            prepared.owner, prepared.account, item
        )
        assert view.preparation == "migration_required" and view.checkpoint_id is None
        assert view.state_id is None
        assert not session.new and not session.dirty and not session.deleted


async def test_system_catalog_and_account_state_are_additive(
    user_client: AsyncClient, stopped: RuntimeHarness
) -> None:
    """
    普通列表仅显式展示系统目录，账户有效列表强制包含并附精确状态说明。

    :param user_client (AsyncClient): 用户认证客户端
    :param stopped (RuntimeHarness): 原会话账户
    """
    plain = await user_client.get("/api/v1/skills")
    assert plain.json()["data"]["system_items"] == []
    catalog = await user_client.get("/api/v1/skills?include_system=true")
    assert {row["name"] for row in catalog.json()["data"]["system_items"]} == {
        "ego-browser",
        "agent-remote-device",
    }
    response = await user_client.get(f"/api/v1/skills?account_id={stopped.account}&effective=true")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["system_items"]) == 2
    state = data["items"][0]["account_state"]
    assert state["state_id"] == str(stopped.state) and state["preparation"] == "initialized"
    assert state["last_recorded_sync_at"] is None and not state["unknown_sync_times"]
    detail = await user_client.get(
        f"/api/v1/skills/installations/learning?account_id={stopped.account}"
    )
    assert detail.json()["data"]["account_state"] == state
    invalid = await user_client.get(f"/api/v1/skills/sessions/{stopped.session}?limit=0")
    assert invalid.status_code == 422


async def test_sync_time_is_original_persistence_evidence(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    内容完成才记录时间，重试及发布不改写，旧空时间不从可变更新时间反推。

    :param stopped (RuntimeHarness): 终态会话
    :param tmp_path (Path): 私有内容卷
    """
    start = datetime.now(UTC)
    identity = await ingest(stopped, tmp_path, {"learning/data": b"learned"})
    async with stopped.database.begin() as session:
        row = await session.get(SkillFinalization, identity)
        assert row is not None and row.persisted_at is not None
        original = row.persisted_at
        assert original.replace(tzinfo=UTC) >= start
        view = await service(session, tmp_path).get(stopped.node, row.id)
        await service(session, tmp_path).complete(stopped.node, row.id, view.upload_id)
        assert row.persisted_at == original
    await publish(stopped, tmp_path, identity)
    async with stopped.database.begin() as session:
        row = await session.get(SkillFinalization, identity)
        assert row is not None and row.persisted_at == original
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.installation_id is not None
        query = SkillEffectiveQueryService(session)
        latest, unknown = await query.repository.sync_times(
            stopped.owner, stopped.account, branch.installation_id
        )
        assert latest == original and not unknown
        row.persisted_at = None
    async with stopped.database.begin() as session:
        latest, unknown = await SkillEffectiveQueryService(session).repository.sync_times(
            stopped.owner, stopped.account, branch.installation_id
        )
        assert latest is None and unknown
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        assert snapshot is not None


async def test_retired_snapshot_keeps_selection_and_original_sync_time(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真实整组退役仅改变保留说明，不改写原成员或首次内容完成时间。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 私有内容卷
    """
    from test_skill_history_retirement import retire

    from agent_remote_server.skill_manager.retention.graph import RetentionKey

    finalization = await ingest(stopped, tmp_path, {})
    publication = await publish(stopped, tmp_path, finalization)
    async with stopped.database.begin() as session:
        before = await SkillEffectiveQueryService(session).session(stopped.owner, stopped.session)
        receipt = await session.get(SkillFinalization, finalization)
        assert receipt is not None
        original_time = receipt.persisted_at
    await retire(
        stopped,
        RetentionKey("snapshot", str(stopped.snapshot)),
        RetentionKey("finalization", str(finalization)),
        RetentionKey("publication", str(publication.id)),
        early=True,
    )
    async with stopped.database.begin() as session:
        after = await SkillEffectiveQueryService(session).session(stopped.owner, stopped.session)
        assert not after.content_retained
        assert after.items == before.items and after.system_items == before.system_items
        assert after.tree_digest == before.tree_digest and after.snapshot_id == before.snapshot_id
        receipt = await session.get(SkillFinalization, finalization)
        assert receipt is not None and receipt.persisted_at == original_time


async def test_failed_completion_never_records_sync_and_outer_rollback_removes_time(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    缺失文件和外层事务回滚均不留下虚假的同步完成时间。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 私有内容卷
    """
    import io

    from test_skill_finalization import request

    payload = request(stopped)
    async with stopped.database.begin() as session:
        plan = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
    with pytest.raises(SkillContentError) as failure:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).complete(stopped.node, plan.id, plan.upload_id)
    assert failure.value.code == "CONTENT_INCOMPLETE"
    async with stopped.database.begin() as session:
        row = await session.get(SkillFinalization, plan.id)
        assert row is not None and row.persisted_at is None
        await service(session, tmp_path).put_file(
            stopped.node,
            plan.id,
            plan.upload_id,
            payload.manifest.entries[1].sha256,
            io.BytesIO(b"---\nname: learning\n---\nlearned"),
        )
    with pytest.raises(RuntimeError, match="rollback"):
        async with stopped.database.begin() as session:
            await service(session, tmp_path).complete(stopped.node, plan.id, plan.upload_id)
            row = await session.get(SkillFinalization, plan.id)
            assert row is not None and row.persisted_at is not None
            raise RuntimeError("rollback")
    async with stopped.database.begin() as session:
        row = await session.get(SkillFinalization, plan.id)
        assert row is not None and row.persisted_at is None and row.status == "upload_pending"


@pytest.mark.parametrize("kind", ["other-user", "device", "node", "disabled"])
async def test_session_query_authorization(
    user_client: AsyncClient, stopped: RuntimeHarness, kind: str
) -> None:
    """
    原会话元数据只向原用户开放，功能关闭仍拒绝读取。

    :param user_client (AsyncClient): 真实认证客户端
    :param stopped (RuntimeHarness): 原始会话
    :param kind (str): 拒绝条件
    """
    from fastapi import FastAPI
    from httpx import ASGITransport
    from test_skill_conflicts_api import token

    from agent_remote_server.api.deps import get_settings
    from agent_remote_server.config import Settings

    headers = {}
    if kind == "disabled":
        assert isinstance(user_client._transport, ASGITransport)
        app = user_client._transport.app
        assert isinstance(app, FastAPI)
        app.dependency_overrides[get_settings] = lambda: Settings(
            secret_key="skill-resolution-test", skill_manager_enabled=False
        )
    else:
        owner = await user(stopped.database) if kind == "other-user" else stopped.owner
        value = await token(stopped, owner, "user" if kind == "other-user" else kind)
        headers = {"Authorization": "Bearer " + value}
    result = await user_client.get(f"/api/v1/skills/sessions/{stopped.session}", headers=headers)
    assert (
        result.status_code == {"other-user": 404, "device": 403, "node": 401, "disabled": 503}[kind]
    )


@pytest.mark.parametrize("kind", ["publication", "migration"])
async def test_conflict_diagnostics_use_real_saved_attempts(
    stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    两个独立冲突域按来源汇总真实未解决尝试，不能将其当作当前启动结论。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param kind (str): 实际冲突域
    """
    from test_skill_migration_conflicts import pending as migration_pending
    from test_skill_resolution_service import pending as publication_pending

    if kind == "publication":
        identity = (await publication_pending(stopped, tmp_path)).id
    else:
        migration = await migration_pending(stopped, tmp_path, mode="forward")
        assert migration.operation_id is not None
        identity = migration.operation_id
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    async with stopped.database.begin() as session:
        item = await library.service(session).info(
            stopped.owner, "learning", scope=SkillScope(account_id=stopped.account)
        )
        view = await SkillEffectiveQueryService(session).account(
            stopped.owner, stopped.account, item
        )
        assert view.publication_conflicts == (1 if kind == "publication" else 0)
        assert view.migration_conflicts == (1 if kind == "migration" else 0)
        assert (
            view.latest_publication_conflict_id
            if kind == "publication"
            else view.latest_migration_conflict_id
        ) == identity
        assert not session.new and not session.dirty and not session.deleted


async def test_session_page_order_is_independent_of_database_language(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    连字符和数字名称必须按统一字节顺序分页，不能受 PostgreSQL 默认语言排序影响。

    :param prepared (RuntimeHarness): 未预约账户
    :param tmp_path (Path): 私有内容卷
    """
    for name in ("learning0", "learning-a", "learning-a1"):
        local = await register(prepared, tmp_path, await source(prepared, tmp_path, name), name)
        await activate(prepared, local)
    await reserve(prepared, tmp_path)
    expected = ["learning", "learning-a", "learning-a1", "learning0"]
    async with prepared.database.begin() as session:
        query = SkillEffectiveQueryService(session)
        cursor = None
        names: list[str] = []
        for _ in expected:
            page = await query.session(prepared.owner, prepared.session, 1, cursor)
            names.extend(item.name for item in page.items)
            cursor = page.next_cursor
        assert cursor is None and names == expected
