"""
验证用户状态历史、原始基线差异、依赖导出和待上传记录的真实 HTTP 边界。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import request, service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_resolution_service import pending
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_library import SkillLibrary
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.manifest import manifest_digest


async def test_history_pagination_and_diff_preserve_explicit_baselines(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    历史区分真实 head 与冲突输入，单项和目录差异分别使用准确原始基线。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    await pending(stopped, tmp_path)
    params: dict[str, str | int] = {
        "account_id": str(stopped.account),
        "skill": "learning",
        "limit": 1,
    }
    rows: list[dict[str, object]] = []
    while True:
        response = await user_client.get("/api/v1/skills/state/checkpoints", params=params)
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        rows.extend(page["items"])
        if page["next_cursor"] is None:
            break
        params["cursor"] = page["next_cursor"]
    assert len(rows) >= 4 and len({row["id"] for row in rows}) == len(rows)
    head = next(row for row in rows if row["is_head"])
    assert any(row["finalization_status"] == "conflicted" and not row["is_head"] for row in rows)
    path = f"/api/v1/skills/state/checkpoints/{head['id']}"
    info = (await user_client.get(path)).json()["data"]
    assert {**info, "storage": None, "retention": None} == head
    assert info["origin"] == "user_library"
    assert info["retention"]["id"] == head["id"]
    assert info["storage"]["scope"] == "user"
    difference = (await user_client.get(path + "/diff", params={"limit": 1})).json()["data"]
    assert difference["base_kind"] == "package_revision"
    assert difference["base_reference_id"] == info["revision_id"]
    assert difference["items"][0]["path"] == "learning/one"
    assert difference["items"][0]["base"] is None and difference["next_cursor"] is not None
    more = (
        await user_client.get(path + "/diff", params={"cursor": difference["next_cursor"]})
    ).json()["data"]
    assert [item["path"] for item in more["items"]] == ["learning/two"]
    directories = (
        await user_client.get(
            "/api/v1/skills/state/checkpoints",
            params={"account_id": str(stopped.account), "scope": "account-directory"},
        )
    ).json()["data"]["items"]
    current_directory = next(row for row in directories if row["is_head"])
    diff = (
        await user_client.get(f"/api/v1/skills/state/checkpoints/{current_directory['id']}/diff")
    ).json()["data"]
    assert (
        diff["base_kind"] == "directory_checkpoint"
        and diff["base_reference_id"] == current_directory["parent_id"]
    )
    cross_scope = await user_client.get(
        "/api/v1/skills/state/checkpoints",
        params={
            "account_id": str(stopped.account),
            "skill": "learning",
            "cursor": current_directory["id"],
        },
    )
    assert cross_scope.status_code == 404


async def test_item_export_retains_link_dependencies_but_not_independent_members(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    单项导出保持原相对链接，声明额外依赖并拒绝其他独立成员文件摘要。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 私有内容卷
    """
    publication = await publish(
        stopped,
        tmp_path,
        await ingest(
            stopped,
            tmp_path,
            {
                "notes/SKILL.md": b"notes",
                "notes/state": b"linked state",
                "independent/SKILL.md": b"unrelated bytes",
            },
            links={"learning/state": "../notes/state"},
        ),
    )
    rows = (
        await user_client.get(
            "/api/v1/skills/state/checkpoints",
            params={"account_id": str(stopped.account), "skill": "learning"},
        )
    ).json()["data"]["items"]
    head = next(row for row in rows if row["is_head"])
    members_path = f"/api/v1/skills/state/checkpoints/{publication.result_checkpoint_id}/members"
    first_members = (await user_client.get(members_path, params={"limit": 2})).json()["data"]
    assert [item["entry_name"] for item in first_members["items"]] == ["independent", "learning"]
    remaining_members = (
        await user_client.get(members_path, params={"cursor": first_members["next_cursor"]})
    ).json()["data"]
    assert [item["entry_name"] for item in remaining_members["items"]] == ["notes"]
    assert first_members["items"][1]["checkpoint_id"] == head["id"]
    path = f"/api/v1/skills/state/checkpoints/{head['id']}"
    exported = (await user_client.get(path + "/tree")).json()["data"]
    assert exported["dependency_roots"] == ["notes"] and not exported["locally_removed"]
    tree = SkillTreeManifest.model_validate(exported["manifest"])
    assert manifest_digest(tree) == exported["tree_digest"]
    assert {entry.path.split("/")[0] for entry in tree.entries} == {"learning", "notes"}
    dependency = next(entry for entry in tree.entries if entry.path == "notes/state")
    assert (await user_client.get(path + "/files/" + dependency.sha256)).content == b"linked state"
    independent = (
        await user_client.get(
            "/api/v1/skills/state/checkpoints",
            params={"account_id": str(stopped.account), "skill": "independent"},
        )
    ).json()["data"]["items"][0]
    foreign_tree = (
        await user_client.get(f"/api/v1/skills/state/checkpoints/{independent['id']}/tree")
    ).json()["data"]
    digest = next(
        entry["sha256"] for entry in foreign_tree["manifest"]["entries"] if entry["kind"] == "file"
    )
    assert (await user_client.get(path + "/files/" + digest)).status_code == 404
    later = await new_session(stopped, tmp_path)
    await publish(later, tmp_path, await ingest(later, tmp_path, {"notes/state": b"changed"}))
    local_rows = (
        await user_client.get(
            "/api/v1/skills/state/checkpoints",
            params={"account_id": str(stopped.account), "skill": "notes"},
        )
    ).json()["data"]["items"]
    local_head = next(row for row in local_rows if row["is_head"])
    diff = (
        await user_client.get(f"/api/v1/skills/state/checkpoints/{local_head['id']}/diff")
    ).json()["data"]
    assert diff["base_kind"] == "local_initial_revision"
    assert [item["path"] for item in diff["items"]] == ["notes/state"]


async def test_deleted_detached_item_remains_queryable_as_explicit_empty_state(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    删除项的异常退出输入也保留单项视图，合法空状态与未上传明确区分。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    receipt = await ingest(stopped, tmp_path, {"learning": None}, unclean=True)
    assert (await publish(stopped, tmp_path, receipt)).status == "detached"
    rows = (
        await user_client.get(
            "/api/v1/skills/state/checkpoints",
            params={"account_id": str(stopped.account), "skill": "learning"},
        )
    ).json()["data"]["items"]
    deleted = next(row for row in rows if row["finalization_status"] == "detached")
    assert not deleted["is_head"] and deleted["retained"] and deleted["invalid_skill_format"]
    path = f"/api/v1/skills/state/checkpoints/{deleted['id']}"
    exported = (await user_client.get(path + "/tree")).json()["data"]
    assert exported["locally_removed"] and exported["manifest"]["entries"] == []
    diff = (await user_client.get(path + "/diff")).json()["data"]
    assert diff["items"] and all(item["current"] is None for item in diff["items"])


async def test_pending_uploads_never_become_empty_exportable_checkpoints(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    待上传列表限定原快照来源，不完整内容无法借用收尾身份导出。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path).begin(
            stopped.node, stopped.snapshot, request(stopped)
        )
    response = await user_client.get(
        "/api/v1/skills/state/pending",
        params={"account_id": str(stopped.account), "skill": "learning"},
    )
    item = response.json()["data"]["items"][0]
    assert item["id"] == str(receipt.id) and item["storage_location"] == "source_node"
    assert not item["exportable_from_server"]
    assert (
        await user_client.get(f"/api/v1/skills/state/checkpoints/{receipt.id}/tree")
    ).status_code == 404
    account = await LibraryHarness(stopped.database, tmp_path, stopped.owner).account()
    wrong = await user_client.get(
        "/api/v1/skills/state/pending",
        params={
            "account_id": str(account),
            "scope": "account-directory",
            "cursor": str(receipt.id),
        },
    )
    assert wrong.status_code == 404


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_checkpoint_content_rejects_other_authorities(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    检查点内容不接受节点、设备或其他用户凭据。

    :param user_client (AsyncClient): 原用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 不应获授权的凭据类型
    """
    result = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/test": b"private"})
    )
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    value = await token(stopped, owner, "user" if kind == "other-user" else kind)
    headers = {"Authorization": "Bearer " + value}
    for suffix in ("", "/tree", "/diff"):
        response = await user_client.get(
            f"/api/v1/skills/state/checkpoints/{result.result_checkpoint_id}" + suffix,
            headers=headers,
        )
        assert response.status_code == (
            404 if kind == "other-user" else 403 if kind == "device" else 401
        )


async def test_expired_tombstone_is_visible_but_content_is_not_synthesized(
    user_client: AsyncClient, stopped: RuntimeHarness
) -> None:
    """
    退役墓碑保留摘要，导出与差异明确失败而不返回空目录。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    """
    identity = uuid4()
    async with stopped.database.begin() as session:
        session.add(
            SkillCheckpoint(
                id=identity,
                user_id=stopped.owner,
                account_id=stopped.account,
                scope="directory",
                content_digest=stopped.tree,
                retained=False,
            )
        )
    path = f"/api/v1/skills/state/checkpoints/{identity}"
    info = (await user_client.get(path)).json()
    assert info["status"] == "state_expired" and info["data"]["storage_location"] == "expired"
    for suffix in ("/tree", "/diff"):
        response = await user_client.get(path + suffix)
        assert (
            response.status_code == 409 and response.json()["errors"][0]["code"] == "STATE_EXPIRED"
        )


async def test_new_user_reads_do_not_create_library_storage_or_branch_state(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    新用户只读空账户历史不能创建存储使用记录或库配置。

    :param user_client (AsyncClient): 共享请求客户端
    :param stopped (RuntimeHarness): 数据库身份
    :param tmp_path (Path): 内容卷
    """
    owner = await user(stopped.database)
    account = await LibraryHarness(stopped.database, tmp_path, owner).account()
    value = await token(stopped, owner)
    for route in ("checkpoints", "pending", "conflicts"):
        response = await user_client.get(
            "/api/v1/skills/state/" + route,
            params={"account_id": str(account), "scope": "account-directory"},
            headers={"Authorization": "Bearer " + value},
        )
        assert response.status_code == 200 and response.json()["data"]["items"] == []
    async with stopped.database() as session:
        assert await session.get(SkillLibrary, owner) is None
        assert await session.get(SkillStorageUsage, owner) is None


async def test_same_named_sources_require_id_and_archived_identity_remains_readable(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同名库与本地来源不能混合历史，稳定身份能读取停用或归档来源。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"notes/SKILL.md": b"local"}))
    path = "/api/v1/skills/state/checkpoints"
    params = {"account_id": str(stopped.account), "skill": "notes"}
    local_rows = (await user_client.get(path, params=params)).json()["data"]["items"]
    local_id = local_rows[0]["skill_id"]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate("notes"))
    ambiguous = await user_client.get(path, params=params)
    assert ambiguous.status_code == 409
    assert ambiguous.json()["errors"][0]["code"] == "SKILL_SOURCE_CONFLICT"
    params["skill"] = local_id
    assert (await user_client.get(path, params=params)).json()["data"]["items"] == local_rows
    other_account = await library.account()
    rejected = await user_client.get(
        path, params={"account_id": str(other_account), "skill": local_id}
    )
    assert rejected.status_code == 404
    assert (
        await user_client.get(
            path,
            params={
                "account_id": str(stopped.account),
                "scope": "account-directory",
                "skill": "notes",
            },
        )
    ).status_code == 422
    learning = await library.info()
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    archived = await user_client.get(
        path, params={"account_id": str(stopped.account), "skill": str(learning.id)}
    )
    assert archived.status_code == 200 and archived.json()["data"]["items"]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_export_rejects_missing_or_corrupt_bytes_before_streaming(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, damage: str
) -> None:
    """
    已保存元数据不能代替实际字节校验，缺失或损坏时不发送成功文件流。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param damage (str): 模拟文件丢失或损坏
    """
    publication = await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/data": b"original bytes"})
    )
    path = f"/api/v1/skills/state/checkpoints/{publication.result_checkpoint_id}"
    exported = (await user_client.get(path + "/tree")).json()["data"]
    entry = next(
        entry for entry in exported["manifest"]["entries"] if entry["path"] == "learning/data"
    )
    digest = entry["sha256"]
    target = tmp_path / "objects" / str(stopped.owner) / digest[:2] / digest
    if damage == "missing":
        target.unlink()
    else:
        target.chmod(0o600)
        target.write_bytes(b"tampered bytes")
    response = await user_client.get(path + "/files/" + digest)
    assert response.status_code == (409 if damage == "missing" else 422)
    assert response.json()["errors"][0]["code"] == (
        "CONTENT_INCOMPLETE" if damage == "missing" else "CONTENT_INVALID"
    )
