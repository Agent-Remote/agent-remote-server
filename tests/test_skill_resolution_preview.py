"""
验证未上传人工清单的只读候选计算，不能借预览获得内容或发布授权。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_directory_merge import directory
from test_skill_finalization import stopped as stopped
from test_skill_migration_conflicts import pending as migration_pending
from test_skill_resolution_service import pending as publication_pending
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionContent,
    SkillMigrationResolutionOperation,
    SkillMigrationResolutionPlan,
    SkillMigrationResolutionUpload,
)
from agent_remote_server.models.skill_resolution import (
    SkillResolutionOperation,
    SkillResolutionPlan,
)
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStoredTree
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.manifest import manifest_digest


async def make_conflict(state: RuntimeHarness, root: Path, kind: str) -> tuple[str, UUID]:
    """
    从真实完整收尾或版本迁移构造可查询原冲突。

    :param state (RuntimeHarness): 已认证账户
    :param root (Path): 内容卷
    :param kind (str): 会话或迁移模式
    :return tuple[str, UUID]: 精确路径和原身份
    """
    if kind == "publication":
        identity = (await publication_pending(state, root)).id
        return f"/api/v1/skills/state/conflicts/{identity}", identity
    migration_id = (await migration_pending(state, root, kind)).operation_id
    assert migration_id is not None
    return f"/api/v1/skills/state/migration/conflicts/{migration_id}", migration_id


async def footprint(
    state: RuntimeHarness, root: Path
) -> tuple[tuple[int | None, ...], tuple[str, ...]]:
    """
    预览不能增加上传、授权、树、计划、操作、检查点或对象文件。

    :param state (RuntimeHarness): 数据库工厂
    :param root (Path): 内容卷
    :return tuple[tuple[int | None, ...], tuple[str, ...]]: 持久对象数量和实际磁盘文件
    """
    async with state.database() as session:
        counts = tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (
                    SkillContentUpload,
                    SkillStoredTree,
                    SkillCheckpoint,
                    SkillResolutionPlan,
                    SkillResolutionOperation,
                    SkillMigrationResolutionPlan,
                    SkillMigrationResolutionOperation,
                    SkillMigrationResolutionUpload,
                    SkillMigrationResolutionContent,
                )
            ]
        )
    files = tuple(
        sorted(str(p.relative_to(root)) for p in (root / "objects").rglob("*") if p.is_file())
    )
    return counts, files


@pytest.mark.parametrize("kind", ["publication", "forward", "incremental"])
async def test_unuploaded_directory_preview_matches_verified_preview_without_writes(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    上传前完整覆盖可预览，但真实发布仍拒绝缺失字节，上传完成后摘要与差异一致。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 原冲突种类
    """
    path, identity = await make_conflict(stopped, tmp_path, kind)
    files = {"learning/SKILL.md": b"# Manually resolved\n", "learning/manual.bin": b"\0\xffdata"}
    manifest = directory(files)
    digest = manifest_digest(manifest)
    choice = {"directory_tree_digest": digest}
    request = {
        "expected_revision": 0,
        "choice": choice,
        "manifest": manifest.model_dump(mode="json"),
    }
    before = await footprint(stopped, tmp_path)
    state = (await command(stopped, tmp_path)).expected
    info = (await user_client.get(path)).json()["data"]
    response = await user_client.post(path + "/content-preview", json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "preview" and not body["committed"] and body["operation_id"] is None
    preview = body["data"]
    assert preview["conflict_id"] == str(identity) and preview["plan_revision"] == 0
    assert (
        preview["metadata_only"]
        and not preview["content_verified"]
        and not preview["ready_to_publish"]
    )
    assert preview["candidate_complete"] and preview["remaining"] == []
    assert preview["proposed_tree_digest"] == preview["result_tree_digest"] == digest
    assert preview["pending_checks"] == [
        "custom_content",
        "source_authorization",
        "quota_admission",
        "head_preconditions",
    ]
    assert "learning/manual.bin" in {item["path"] for item in preview["changes"]}
    if kind != "publication":
        assert preview["target_revision_id"] == info["current"]["revision_id"]
        assert preview["target_modified"] and preview["original_changes"]
    assert before == await footprint(stopped, tmp_path)
    assert state == (await command(stopped, tmp_path)).expected
    assert (await user_client.get(path)).json()["data"] == info
    actual = {
        "idempotency_key": str(uuid4()),
        "expected_revision": 0,
        "choice": choice,
        "dry_run": True,
    }
    denied = await user_client.post(path + "/resolve", json=actual)
    assert denied.status_code == 404
    assert denied.json()["errors"][0]["code"] in {
        "CONTENT_NOT_FOUND",
        "RESOLUTION_CONTENT_NOT_FOUND",
    }
    assert before == await footprint(stopped, tmp_path)
    upload = await user_client.post(
        path + "/uploads", json={"idempotency_key": str(uuid4()), "manifest": request["manifest"]}
    )
    assert upload.status_code == 200, upload.text
    upload_path = f"{path}/uploads/{upload.json()['data']['id']}"
    for entry in manifest.entries:
        if entry.kind == "file":
            assert (
                await user_client.put(
                    upload_path + "/files/" + entry.sha256, content=files[entry.path]
                )
            ).status_code == 200
    assert (await user_client.post(upload_path + "/complete")).status_code == 200
    verified = await user_client.post(path + "/resolve", json=actual)
    assert verified.status_code == 200, verified.text
    assert verified.json()["data"]["result_tree_digest"] == preview["result_tree_digest"]
    if kind != "publication":
        assert verified.json()["data"]["original_changes"] == preview["original_changes"]
        assert verified.json()["data"]["directory_changes"] == preview["changes"]
        assert verified.json()["data"]["target_changes"] == preview["target_changes"]
    actual["dry_run"] = False
    published = await user_client.post(path + "/resolve", json=actual)
    assert published.status_code == 200 and published.json()["status"] == "published", (
        published.text
    )
    assert published.json()["data"]["result_tree_digest"] == preview["result_tree_digest"]


async def test_file_preview_keeps_remaining_paths_and_saved_choices(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    部分人工文件预览不保存选择，随后基于已保存计划可预览完整结果。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    path, _ = await make_conflict(stopped, tmp_path, "publication")
    manifest = SkillTreeManifest(entries=(file_entry(b"manual", path="content"),))
    request = {
        "expected_revision": 0,
        "choice": {"path": "learning/one", "file_tree_digest": manifest_digest(manifest)},
        "manifest": manifest.model_dump(mode="json"),
    }
    before = await footprint(stopped, tmp_path)
    partial = (await user_client.post(path + "/content-preview", json=request)).json()["data"]
    assert not partial["candidate_complete"] and partial["result_tree_digest"] is None
    assert partial["changes"] is None and partial["remaining"][0]["path"] == "learning/two"
    assert before == await footprint(stopped, tmp_path)
    saved = await user_client.post(
        path + "/resolve",
        json={
            "idempotency_key": str(uuid4()),
            "expected_revision": 0,
            "choice": {"path": "learning/two", "use": "incoming"},
        },
    )
    assert saved.json()["status"] == "pending"
    stale = await user_client.post(path + "/content-preview", json=request)
    assert (
        stale.status_code == 409 and stale.json()["errors"][0]["code"] == "PLAN_REVISION_CONFLICT"
    )
    request["expected_revision"] = 1
    before = await footprint(stopped, tmp_path)
    full = (await user_client.post(path + "/content-preview", json=request)).json()["data"]
    assert full["candidate_complete"] and len(full["choices"]) == 2 and full["plan_revision"] == 1
    assert before == await footprint(stopped, tmp_path)


@pytest.mark.parametrize("kind", ["publication", "incremental"])
@pytest.mark.parametrize("authority", ["other-user", "device", "node"])
async def test_content_preview_requires_original_owner(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str, authority: str
) -> None:
    """
    未授权调用不能借清单预览读取真实原输入或创建任何持久对象。

    :param user_client (AsyncClient): 原用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 冲突域
    :param authority (str): 拒绝的凭据类型
    """
    path, _ = await make_conflict(stopped, tmp_path, kind)
    owner = await user(stopped.database) if authority == "other-user" else stopped.owner
    secret = await token(stopped, owner, "user" if authority == "other-user" else authority)
    before = await footprint(stopped, tmp_path)
    response = await user_client.post(
        path + "/content-preview",
        content=b"invalid body",
        headers={"Authorization": "Bearer " + secret},
    )
    assert response.status_code == {"other-user": 404, "device": 403, "node": 401}[authority]
    assert before == await footprint(stopped, tmp_path)


@pytest.mark.parametrize("kind", ["publication", "incremental"])
async def test_preview_rejects_invalid_manifest_choice_and_expired_plan(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    摘要不匹配、实际命令字段和失效原尝试均不能用预览绕过。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 冲突域
    """
    path, _ = await make_conflict(stopped, tmp_path, kind)
    manifest = SkillTreeManifest(entries=())
    request = {
        "expected_revision": 0,
        "choice": {"directory_tree_digest": manifest_digest(manifest)},
        "manifest": manifest.model_dump(mode="json"),
    }
    before = await footprint(stopped, tmp_path)
    for bad in [
        {**request, "choice": {"directory_tree_digest": "a" * 64}},
        {**request, "choice": {"use": "current"}},
        {**request, "dry_run": False},
        {**request, "expected_revision": True},
    ]:
        response = await user_client.post(path + "/content-preview", json=bad)
        assert response.status_code == 422, response.text
    assert before == await footprint(stopped, tmp_path)
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    before = await footprint(stopped, tmp_path)
    stale = await user_client.post(path + "/content-preview", json=request)
    assert stale.status_code == 409, stale.text
    assert before == await footprint(stopped, tmp_path)


@pytest.mark.parametrize("kind", ["publication", "incremental"])
async def test_preview_transport_and_declared_content_are_bounded(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    按实际流长度限制元数据正文，并在加载比较前拒绝声明超额内容。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 冲突域
    :param monkeypatch (pytest.MonkeyPatch): 临时传输边界
    """
    path, _ = await make_conflict(stopped, tmp_path, kind)
    before = await footprint(stopped, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr("agent_remote_server.api.skill_resolution_previews._MAX_MANIFEST_BYTES", 32)
        response = await user_client.post(path + "/content-preview", content=b"x" * 33)
        assert response.status_code == 413
        assert response.json()["errors"][0]["code"] == "CONTENT_TOO_LARGE"
    entry = file_entry(b"large", path="content").model_copy(update={"size": 11 * 1024**3})
    manifest = SkillTreeManifest(entries=(entry,))
    response = await user_client.post(
        path + "/content-preview",
        json={
            "expected_revision": 0,
            "choice": {"directory_tree_digest": manifest_digest(manifest)},
            "manifest": manifest.model_dump(mode="json"),
        },
    )
    assert response.status_code == 413 and response.json()["errors"][0]["code"] == "QUOTA_EXCEEDED"
    assert before == await footprint(stopped, tmp_path)


async def test_metadata_preview_does_not_authorize_an_independent_migration_member(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    人工目录中的额外根不能通过纯清单入口伪装为可写迁移范围。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    path, _ = await make_conflict(stopped, tmp_path, "incremental")
    manifest = directory({"learning/SKILL.md": b"custom", "unrelated/SKILL.md": b"new source"})
    before = await footprint(stopped, tmp_path)
    response = await user_client.post(
        path + "/content-preview",
        json={
            "expected_revision": 0,
            "choice": {"directory_tree_digest": manifest_digest(manifest)},
            "manifest": manifest.model_dump(mode="json"),
        },
    )
    assert (
        response.status_code == 409 and response.json()["errors"][0]["code"] == "INVALID_RESOLUTION"
    )
    assert before == await footprint(stopped, tmp_path)


@pytest.mark.parametrize("kind", ["forward", "incremental"])
async def test_migration_file_preview_keeps_new_revision_and_learned_data(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    单文件候选保留目标确切原始版本和其他已学内容，不冒充未修改新版。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 准备或增量迁移
    """
    path, _ = await make_conflict(stopped, tmp_path, kind)
    info = (await user_client.get(path)).json()["data"]
    manifest = SkillTreeManifest(entries=(file_entry(b"manual instructions", path="content"),))
    request = {
        "expected_revision": 0,
        "choice": {"path": "learning/SKILL.md", "file_tree_digest": manifest_digest(manifest)},
        "manifest": manifest.model_dump(mode="json"),
    }
    before = await footprint(stopped, tmp_path)
    response = await user_client.post(path + "/content-preview", json=request)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["candidate_complete"] and data["target_modified"]
    assert data["target_revision_id"] == info["current"]["revision_id"]
    assert {item["path"] for item in data["original_changes"]} == {
        "learning/SKILL.md",
        "learning/one",
    }
    assert not data["ready_to_publish"] and before == await footprint(stopped, tmp_path)


async def test_preview_rejects_file_for_deletion_conflict(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    删除对修改的结构冲突必须选完整侧，不能借未上传清单入口强行新增文件。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/file": b"base"}))
    left = await new_session(stopped, tmp_path)
    right = await new_session(stopped, tmp_path)
    await publish(left, tmp_path, await ingest(left, tmp_path, {"learning/file": b"changed"}))
    publication = await publish(
        right, tmp_path, await ingest(right, tmp_path, {"learning/file": None})
    )
    assert publication.status == "conflicted"
    path = f"/api/v1/skills/state/conflicts/{publication.id}/content-preview"
    manifest = SkillTreeManifest(entries=(file_entry(b"manual", path="content"),))
    before = await footprint(stopped, tmp_path)
    response = await user_client.post(
        path,
        json={
            "expected_revision": 0,
            "choice": {"path": "learning/file", "file_tree_digest": manifest_digest(manifest)},
            "manifest": manifest.model_dump(mode="json"),
        },
    )
    assert (
        response.status_code == 409 and response.json()["errors"][0]["code"] == "INVALID_RESOLUTION"
    )
    assert before == await footprint(stopped, tmp_path)
