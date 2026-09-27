"""
验证真实用户认证下的冲突查询、人工内容上传、导出和解决闭环。
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_resolution_service import pending, upload_tree
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.api.deps import get_session
from agent_remote_server.config import Settings
from agent_remote_server.main import create_app
from agent_remote_server.models import AuthToken, Node, UserDevice
from agent_remote_server.models.skill_publications import SkillPublicationBranch
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.security.tokens import hash_token


async def token(state: RuntimeHarness, owner: UUID, kind: str = "user") -> str:
    """
    写入真实认证记录，测试不覆盖用户或令牌认证依赖。

    :param state (RuntimeHarness): 数据库工厂及节点身份
    :param owner (UUID): 令牌归属用户
    :param kind (str): 用户、设备或节点令牌类型
    :return str: 仅测试使用的原始令牌
    """
    value = "resolution-test:" + str(uuid4())
    async with state.database.begin() as session:
        if kind == "node":
            node = await session.get(Node, state.node)
            assert node is not None
            node.node_token_hash = hash_token("skill-resolution-test", value)
        else:
            device = None
            if kind == "device":
                row = UserDevice(user_id=owner, name="测试设备", platform="linux", status="active")
                session.add(row)
                await session.flush()
                device = row.id
            session.add(
                AuthToken(
                    user_id=owner,
                    user_device_id=device,
                    token_type=kind,
                    status="active",
                    token_hash=hash_token("skill-resolution-test", value),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    return value


@pytest.fixture
async def user_client(stopped: RuntimeHarness, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """
    仅替换请求数据库，保留真实用户令牌和功能开关验证。

    :param stopped (RuntimeHarness): 已保留快照的测试会话
    :param tmp_path (Path): 私有内容卷
    :return AsyncIterator[AsyncClient]: 真实用户授权客户端
    """
    settings = Settings(
        secret_key="skill-resolution-test",
        log_level="CRITICAL",
        skill_manager_enabled=True,
        skill_storage_root=tmp_path / "objects",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    app = create_app(settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        提供独立请求事务，错误由关闭会话回滚。

        :return AsyncIterator[AsyncSession]: 当前请求数据库会话
        """
        async with stopped.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    value = await token(stopped, stopped.owner)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {value}"},
    ) as client:
        yield client
    await app.state.database_engine.dispose()


async def test_user_can_inspect_export_upload_and_resolve_without_confusing_sides(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    全程通过 HTTP 读取明确三侧、导出输入并上传人工文件，然后原子解决两步计划。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await pending(stopped, tmp_path)
    path = f"/api/v1/skills/state/conflicts/{publication.id}"
    info = await user_client.get(path)
    assert info.status_code == 200, info.text
    data = info.json()["data"]
    assert data["base"]["source"] == "session_snapshot"
    assert data["current"]["source"] == "publication_comparison"
    assert data["incoming"]["source"] == "finalization"
    assert data["scope"] == "account-directory" and data["plan_revision"] == 0
    diff = (await user_client.get(path + "/diff", params={"limit": 1})).json()["data"]
    assert len(diff["items"]) == 1 and diff["next_cursor"] is not None
    more = (
        await user_client.get(path + "/diff", params={"cursor": diff["next_cursor"], "limit": 1})
    ).json()["data"]
    assert len(more["items"]) == 1 and more["next_cursor"] is None
    incoming = (await user_client.get(path + "/trees/incoming")).json()["data"]
    entry = next(
        entry for entry in incoming["manifest"]["entries"] if entry["path"] == "learning/one"
    )
    exported = await user_client.get(path + "/trees/incoming/files/" + entry["sha256"])
    assert exported.status_code == 200 and exported.content == b"right1"
    content = b"manual resolution"
    replacement = file_entry(content, path="content")
    tree = SkillTreeManifest(entries=(replacement,))
    request = {"idempotency_key": "manual-upload", "manifest": tree.model_dump(mode="json")}
    begin = await user_client.post(path + "/uploads", json=request)
    assert begin.status_code == 200, begin.text
    upload_id = begin.json()["data"]["id"]
    upload_path = path + "/uploads/" + upload_id
    assert (await user_client.post(path + "/uploads", json=request)).json() == begin.json()
    assert (await user_client.post(upload_path + "/complete")).status_code == 409
    invalid = await user_client.put(
        upload_path + "/files/" + replacement.sha256, content=b"x" * len(content)
    )
    assert invalid.status_code == 422 and invalid.json()["errors"][0]["code"] == "CONTENT_INVALID"
    assert (await user_client.post(upload_path + "/complete")).status_code == 409
    assert (
        await user_client.put(upload_path + "/files/" + replacement.sha256, content=content)
    ).status_code == 200
    complete = await user_client.post(upload_path + "/complete")
    assert complete.status_code == 200, complete.text
    digest = complete.json()["data"]["tree_digest"]
    selection = {
        "idempotency_key": "choice-one",
        "expected_revision": 0,
        "choice": {"path": "learning/one", "file_tree_digest": digest},
    }
    preview = await user_client.post(
        path + "/resolve",
        json={
            "idempotency_key": "preview-only",
            "expected_revision": 0,
            "choice": {"use": "current"},
            "dry_run": True,
        },
    )
    assert preview.status_code == 200 and preview.json()["data"]["ready"]
    assert not preview.json()["committed"] and preview.json()["operation_id"] is None
    assert (await user_client.get(path)).json()["data"]["plan_revision"] == 0
    assert (
        await user_client.get(
            "/api/v1/skills/state/resolution-operations", params={"key": "preview-only"}
        )
    ).status_code == 404
    first = await user_client.post(path + "/resolve", json=selection)
    assert first.status_code == 200 and first.json()["status"] == "pending", first.text
    second = await user_client.post(
        path + "/resolve",
        json={
            "idempotency_key": "choice-two",
            "expected_revision": 1,
            "choice": {"path": "learning/two", "use": "incoming"},
        },
    )
    assert second.status_code == 200 and second.json()["status"] == "published", second.text
    assert (await user_client.post(path + "/resolve", json=selection)).json() == first.json()
    recovered = await user_client.get(
        "/api/v1/skills/state/resolution-operations", params={"key": "choice-one"}
    )
    assert recovered.json() == first.json()
    by_id = await user_client.get(
        f"/api/v1/skills/state/resolution-operations/{first.json()['operation_id']}"
    )
    assert by_id.status_code == 200 and by_id.json() == recovered.json()
    absent = await user_client.get(f"/api/v1/skills/state/resolution-operations/{publication.id}")
    assert absent.status_code == 404 and absent.json()["errors"][0]["code"] == "OPERATION_NOT_FOUND"

    assert (await user_client.get(path)).json()["data"]["plan_revision"] == 2


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_conflict_http_rejects_other_authorities(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    kind: str,
) -> None:
    """
    真实其他用户、设备和节点凭据均不能查询或修改当前用户冲突。

    :param user_client (AsyncClient): 原始用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 待拒绝认证类别
    """
    publication = await pending(stopped, tmp_path)
    accepted = await user_client.post(
        f"/api/v1/skills/state/conflicts/{publication.id}/resolve",
        json={
            "idempotency_key": "owner-choice",
            "expected_revision": 0,
            "choice": {"path": "learning/one", "use": "current"},
        },
    )
    assert accepted.status_code == 200
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    value = await token(stopped, owner, "user" if kind == "other-user" else kind)
    headers = {"Authorization": f"Bearer {value}"}
    hidden = await user_client.get(
        f"/api/v1/skills/state/resolution-operations/{accepted.json()['operation_id']}",
        headers=headers,
    )
    assert hidden.status_code == (404 if kind == "other-user" else 403 if kind == "device" else 401)
    path = f"/api/v1/skills/state/conflicts/{publication.id}"
    for suffix in ("", "/diff", "/trees/incoming"):
        response = await user_client.get(path + suffix, headers=headers)
        assert response.status_code == (
            404 if kind == "other-user" else (403 if kind == "device" else 401)
        ), response.text
    response = await user_client.post(
        path + "/resolve",
        headers=headers,
        json={
            "idempotency_key": "unauthorized",
            "expected_revision": 0,
            "choice": {"use": "incoming"},
        },
    )
    assert response.status_code in {401, 403, 404}
    response = await user_client.post(
        path + "/uploads",
        headers=headers,
        json={"idempotency_key": "unauthorized-upload", "manifest": {"entries": []}},
    )
    assert response.status_code in {401, 403, 404}


async def test_pagination_upload_binding_and_foreign_custom_tree_are_scoped(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    游标有界且账户明确，另一个冲突的上传或另一个用户的人工树不能被引用。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await pending(stopped, tmp_path)
    receipt = await ingest(other, tmp_path, {"learning/one": b"third session"})
    second = await publish(other, tmp_path, receipt)
    assert second.status == "conflicted"
    path = f"/api/v1/skills/state/conflicts/{first.id}"
    page = (
        await user_client.get(
            "/api/v1/skills/state/conflicts",
            params={"account_id": str(stopped.account), "limit": 1},
        )
    ).json()["data"]
    assert len(page["items"]) == 1 and page["next_cursor"] is not None
    next_page = (
        await user_client.get(
            "/api/v1/skills/state/conflicts",
            params={"account_id": str(stopped.account), "limit": 1, "cursor": page["next_cursor"]},
        )
    ).json()["data"]
    assert (
        next_page["items"][0]["id"] != page["items"][0]["id"] and next_page["next_cursor"] is None
    )
    other_account = await LibraryHarness(stopped.database, tmp_path, stopped.owner).account()
    cross_account = await user_client.get(
        "/api/v1/skills/state/conflicts",
        params={"account_id": str(other_account), "cursor": page["next_cursor"]},
    )
    assert cross_account.status_code == 404
    uploaded = await user_client.post(
        path + "/uploads", json={"idempotency_key": "scoped", "manifest": {"entries": []}}
    )
    upload_id = uploaded.json()["data"]["id"]
    wrong = await user_client.get(f"/api/v1/skills/state/conflicts/{second.id}/uploads/{upload_id}")
    assert wrong.status_code == 404
    foreign_owner = await user(stopped.database)
    foreign_tree = await upload_tree(
        replace(stopped, owner=foreign_owner), tmp_path, {"content": b"other user private content"}
    )
    response = await user_client.post(
        path + "/resolve",
        json={
            "idempotency_key": "foreign-tree",
            "expected_revision": 0,
            "choice": {"path": "learning/one", "file_tree_digest": foreign_tree},
        },
    )
    assert (
        response.status_code == 404 and response.json()["errors"][0]["code"] == "CONTENT_NOT_FOUND"
    )
    assert (await user_client.get(path)).json()["data"]["plan_revision"] == 0


async def test_source_filter_precedes_pagination_and_binds_cursor(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    在分页前筛选稳定来源，未改动的观察分支也保留，跨来源游标不可复用。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    learning = await library.info()
    late = await new_session(stopped, tmp_path)
    latest = await new_session(stopped, tmp_path)
    first = await pending(stopped, tmp_path)
    second = await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/one": b"two"}))
    third = await publish(
        latest, tmp_path, await ingest(latest, tmp_path, {"learning/one": b"three"})
    )
    assert second.status == third.status == "conflicted"
    await library.add(await library.candidate("unrelated"))
    unrelated = await library.info("unrelated")
    async with stopped.database.begin() as session:
        branch = AccountSkillState(
            user_id=stopped.owner,
            account_id=stopped.account,
            installation_id=unrelated.id,
            installation_epoch=unrelated.epoch,
            base_revision_id=unrelated.default_revision_id,
        )
        session.add(branch)
        await session.flush()
        newest = await session.get(SkillPublicationBranch, (third.id, stopped.state))
        observed = await session.get(SkillPublicationBranch, (second.id, stopped.state))
        assert newest is not None and observed is not None
        newest.state_id = branch.id
        newest.expected_checkpoint_id = None
        newest.entry_name = "unrelated"
        observed.changed = False
    path = "/api/v1/skills/state/conflicts"
    params: dict[str, str | int] = {
        "account_id": str(stopped.account),
        "skill": str(learning.id),
        "limit": 1,
    }
    page = (await user_client.get(path, params=params)).json()["data"]
    assert [row["id"] for row in page["items"]] == [str(second.id)]
    assert page["next_cursor"] == str(second.id)
    more = (await user_client.get(path, params={**params, "cursor": page["next_cursor"]})).json()
    assert [row["id"] for row in more["data"]["items"]] == [str(first.id)]
    assert more["data"]["next_cursor"] is None
    named = (await user_client.get(path, params={**params, "skill": "learning"})).json()["data"]
    assert named == page
    denied = await user_client.get(path, params={**params, "cursor": str(third.id)})
    assert denied.status_code == 404 and denied.json()["errors"][0]["code"] == "CONFLICT_NOT_FOUND"
    unfiltered = (await user_client.get(path, params={"account_id": str(stopped.account)})).json()
    assert len(unfiltered["data"]["items"]) == 3


async def test_local_observed_source_and_ambiguous_name_conflicts(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    完整目录冲突关联未改动本地来源，同名库来源不能绕过稳定身份选择。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"notes/SKILL.md": b"local"}))
    state = await new_session(stopped, tmp_path)
    publication = await pending(state, tmp_path)
    path = "/api/v1/skills/state/conflicts"
    params = {"account_id": str(stopped.account), "skill": "notes"}
    page = (await user_client.get(path, params=params)).json()["data"]
    assert [row["id"] for row in page["items"]] == [str(publication.id)]
    info = (await user_client.get(f"{path}/{publication.id}")).json()["data"]
    assert not next(row for row in info["branches"] if row["entry_name"] == "notes")["changed"]
    source = (
        await user_client.get(
            "/api/v1/skills/installations/notes", params={"account_id": str(stopped.account)}
        )
    ).json()["data"]["id"]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate("notes"))
    ambiguous = await user_client.get(path, params=params)
    assert ambiguous.status_code == 409
    assert ambiguous.json()["errors"][0]["code"] == "SKILL_SOURCE_CONFLICT"
    assert (await user_client.get(path, params={**params, "skill": source})).json()["data"] == page
    other_account = await library.account()
    denied = await user_client.get(path, params={"account_id": str(other_account), "skill": source})
    assert denied.status_code == 404
