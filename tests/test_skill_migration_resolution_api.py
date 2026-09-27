"""
验证用户迁移解决的 HTTP 闭环、不可变类型回执、严格输入及认证范围。
"""

import asyncio
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
from test_skill_finalization import stopped as stopped
from test_skill_migration_conflicts import BASE, counts, pending
from test_skill_migration_resolution_content import begin
from test_skill_migration_resolution_drafts import edit, edit_request, saved_plan, two_conflicts
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_migration_resolution import SkillMigrationResolutionOperation
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_migration_resolution import (
    SkillMigrationResolutionReceipt,
    SkillMigrationResolutionView,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError

OPERATIONS = "/api/v1/skills/state/migration/resolution-operations"


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_http_preview_publish_replay_and_typed_receipt(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, mode: str
) -> None:
    """
    真实用户请求完成整个迁移发布，预览零写入且原受理输入始终不变。

    :param user_client (AsyncClient): 真实令牌客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param mode (str): 首次向前或显式增量迁移
    """
    original = await pending(stopped, tmp_path, mode)
    identity = original.operation_id
    assert identity is not None
    path = f"{BASE}/{identity}"
    payload = edit_request()
    rows = await counts(stopped)
    preview = await user_client.post(
        path + "/resolve", json=payload.model_copy(update={"dry_run": True}).model_dump(mode="json")
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["schema_version"] == 1 and body["status"] == "preview" and not body["committed"]
    assert body["operation_id"] is None and not body["errors"]
    assert await counts(stopped) == rows and await saved_plan(stopped, identity) == (0, [], 0)
    assert (
        await user_client.get(OPERATIONS, params={"key": payload.idempotency_key})
    ).status_code == 404
    response, concurrent = await asyncio.gather(
        user_client.post(path + "/resolve", json=payload.model_dump(mode="json")),
        user_client.post(path + "/resolve", json=payload.model_dump(mode="json")),
    )
    assert response.status_code == concurrent.status_code == 200, response.text
    assert response.json() == concurrent.json()
    result = SkillMigrationResolutionView.model_validate(response.json()["data"])
    assert response.json()["status"] == "published" and response.json()["committed"]
    assert response.json()["operation_id"] == str(result.operation_id)
    assert result.operation_kind == "migration_resolution" and result.migration_sequence == 1
    assert result.result_tree_digest == body["data"]["result_tree_digest"]
    assert (
        await user_client.post(path + "/resolve", json=payload.model_dump(mode="json"))
    ).json() == response.json()
    receipt_response = await user_client.get(OPERATIONS, params={"key": payload.idempotency_key})
    by_id = await user_client.get(f"{OPERATIONS}/{result.operation_id}")
    assert by_id.status_code == 200 and by_id.json() == receipt_response.json()
    absent = await user_client.get(f"{OPERATIONS}/{identity}")
    assert absent.status_code == 404 and absent.json()["errors"][0]["code"] == "OPERATION_NOT_FOUND"
    receipt = SkillMigrationResolutionReceipt.model_validate(receipt_response.json()["data"])
    assert receipt.result == result and receipt.current_status == "ready"
    info = (await user_client.get(path)).json()["data"]
    assert info["original"] == original.model_dump(mode="json") and info["status"] == "ready"
    assert not info["live"]["recomputation_reasons"]
    fresh = await user_client.post(
        path + "/resolve", json=edit_request(revision=1).model_dump(mode="json")
    )
    assert fresh.status_code == 409 and fresh.json()["errors"][0]["code"] == "CONFLICT_NOT_ACTIVE"


async def test_http_custom_upload_pending_plan_and_original_pending_receipt(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    人工文件先按迁移上传，部分计划不发布，后续完成不能改写早期 pending 回执。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    path = f"{BASE}/{identity}"
    content = b"manual resolution"
    upload = await begin(user_client, identity, content)
    upload_path = f"{path}/uploads/{upload}"
    put = await user_client.put(
        upload_path + "/files/" + file_entry(content).sha256, content=content
    )
    assert put.status_code == 200
    completed = await user_client.post(upload_path + "/complete")
    assert completed.status_code == 200
    digest = completed.json()["data"]["tree_digest"]
    payload = edit_request(SkillResolutionChoice(path="learning/a", file_tree_digest=digest))
    before = (await command(stopped, tmp_path)).expected
    first = await user_client.post(path + "/resolve", json=payload.model_dump(mode="json"))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending" and first.json()["committed"]
    assert (await command(stopped, tmp_path)).expected == before
    plan = (await user_client.get(path + "/plan")).json()["data"]
    assert plan["revision"] == 1 and len(plan["choices"]) == 1
    second = edit_request(SkillResolutionChoice(path="learning/b", use="current"), revision=1)
    published = await user_client.post(path + "/resolve", json=second.model_dump(mode="json"))
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert published.json()["data"]["migration_sequence"] == 2
    assert (
        await user_client.post(path + "/resolve", json=payload.model_dump(mode="json"))
    ).json() == first.json()
    receipt = (await user_client.get(OPERATIONS, params={"key": payload.idempotency_key})).json()
    assert receipt["status"] == "pending" and receipt["data"]["current_status"] == "ready"
    assert receipt["data"]["result"] == first.json()["data"]
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert (
        await user_client.post(path + "/resolve", json=second.model_dump(mode="json"))
    ).json() == published.json()


@pytest.mark.parametrize("kind", ["device", "node", "other-user", "anonymous"])
async def test_resolve_and_receipt_require_owner_user_credentials(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    持有冲突身份或用户键不授权其他凭据访问，也不能留下计划或回执。

    :param user_client (AsyncClient): 正常用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param kind (str): 无权限凭据类别
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    payload = edit_request()
    accepted = await edit(stopped, tmp_path, identity, payload)
    before = await saved_plan(stopped, identity)
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    value = (
        ""
        if kind == "anonymous"
        else "Bearer " + await token(stopped, owner, "user" if kind == "other-user" else kind)
    )
    headers = {"Authorization": value}
    denied = await user_client.post(
        f"{BASE}/{identity}/resolve",
        headers=headers,
        json=edit_request(revision=1).model_dump(mode="json"),
    )
    receipt = await user_client.get(
        OPERATIONS, params={"key": payload.idempotency_key}, headers=headers
    )
    expected = 404 if kind == "other-user" else 403 if kind == "device" else 401
    by_id = await user_client.get(f"{OPERATIONS}/{accepted.operation_id}", headers=headers)
    assert by_id.status_code == expected
    assert denied.status_code == receipt.status_code == expected
    assert await saved_plan(stopped, identity) == before


async def test_http_plan_version_key_and_operation_type_boundaries(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    错误版本、不同请求复用键和草稿回执不能被解析成成功的最终发布。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    identity = await two_conflicts(stopped, tmp_path)
    path = f"{BASE}/{identity}/resolve"
    draft_payload = edit_request(SkillResolutionChoice(path="learning/a", use="current"))
    await edit(stopped, tmp_path, identity, draft_payload)
    wrong_type = await user_client.get(OPERATIONS, params={"key": draft_payload.idempotency_key})
    assert (
        wrong_type.status_code == 409
        and wrong_type.json()["errors"][0]["code"] == "OPERATION_KIND_MISMATCH"
    )
    stale = await user_client.post(path, json=edit_request().model_dump(mode="json"))
    assert (
        stale.status_code == 409 and stale.json()["errors"][0]["code"] == "PLAN_REVISION_CONFLICT"
    )
    collision = await user_client.post(path, json=draft_payload.model_dump(mode="json"))
    assert (
        collision.status_code == 409
        and collision.json()["errors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    payload = edit_request(SkillResolutionChoice(path="learning/a", use="incoming"), revision=1)
    first = await user_client.post(path, json=payload.model_dump(mode="json"))
    assert first.status_code == 200 and first.json()["status"] == "pending"
    altered = payload.model_copy(update={"choice": SkillResolutionChoice(use="current")})
    collision = await user_client.post(path, json=altered.model_dump(mode="json"))
    assert (
        collision.status_code == 409
        and collision.json()["errors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert (
        await user_client.post(path, json=payload.model_dump(mode="json"))
    ).json() == first.json()


async def test_http_stale_and_reset_comparisons_cannot_publish_choices(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    过期重算创建独立空计划，重置后的旧输入只能保留而不能暗中应用提交选择。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    newer = await publish(
        other, tmp_path, await ingest(other, tmp_path, {"learning/new": b"new head"})
    )
    assert newer.status == "published"
    path = f"{BASE}/{identity}/resolve"
    before = await counts(stopped)
    payload = edit_request()
    preview = await user_client.post(
        path, json=payload.model_copy(update={"dry_run": True}).model_dump(mode="json")
    )
    assert preview.status_code == 200 and preview.json()["status"] == "preview"
    assert preview.json()["data"]["recomputation_possible"] and not preview.json()["committed"]
    assert await counts(stopped) == before and await saved_plan(stopped, identity) == (0, [], 0)
    response = await user_client.post(path, json=payload.model_dump(mode="json"))
    assert response.status_code == 200 and response.json()["status"] == "superseded"
    replacement = response.json()["data"]["replacement_id"]
    assert replacement is not None
    plan = (await user_client.get(f"{BASE}/{replacement}/plan")).json()["data"]
    assert plan["revision"] == 0 and plan["choices"] == []
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    reset = await user_client.post(
        f"{BASE}/{replacement}/resolve", json=edit_request().model_dump(mode="json")
    )
    assert reset.status_code == 200 and reset.json()["status"] == "superseded"
    assert reset.json()["data"]["replacement_id"] is None
    assert reset.json()["data"]["stale_reasons"] == ["state_reset"]
    assert (
        await user_client.post(path, json=payload.model_dump(mode="json"))
    ).json() == response.json()
    receipt = (await user_client.get(OPERATIONS, params={"key": payload.idempotency_key})).json()[
        "data"
    ]
    assert receipt["replacement_id"] == replacement and receipt["current_status"] == "superseded"


async def test_http_strict_choice_and_foreign_content_validation(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    参数不能混用、越界或携带未授权内容摘要，所有拒绝均不产生解决计划。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    path = f"{BASE}/{identity}/resolve"
    invalid: list[dict[str, object]] = [
        {"choice": {"use": "current", "directory_tree_digest": "0" * 64}},
        {"choice": {"file_tree_digest": "0" * 64}},
        {"choice": {"path": "../escape", "use": "current"}},
        {"expected_revision": True},
        {"dry_run": "false"},
        {"user_id": str(stopped.owner)},
    ]
    for change in invalid:
        response = await user_client.post(
            path, json={**edit_request().model_dump(mode="json"), **change}
        )
        assert response.status_code == 422
    missing = await user_client.post(
        path,
        json=edit_request(SkillResolutionChoice(directory_tree_digest="0" * 64)).model_dump(
            mode="json"
        ),
    )
    assert (
        missing.status_code == 404
        and missing.json()["errors"][0]["code"] == "RESOLUTION_CONTENT_NOT_FOUND"
    )
    assert (await user_client.get(OPERATIONS, params={"key": " "})).status_code == 422
    assert (await user_client.get(OPERATIONS, params={"key": str(uuid4())})).status_code == 404
    assert await saved_plan(stopped, identity) == (0, [], 0)


async def test_http_late_receipt_failure_rolls_back_publication_and_accepts_retry(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    回执写入后的故障不能向客户端返回成功，重试同键仍能完成唯一发布。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 晚到数据库故障注入
    """
    original = await pending(stopped, tmp_path)
    identity = original.operation_id
    assert identity is not None
    before = (await command(stopped, tmp_path)).expected
    rows = await counts(stopped)
    save = SkillMigrationResolutionRepository.save_operation

    async def reject(
        self: SkillMigrationResolutionRepository, operation: SkillMigrationResolutionOperation
    ) -> None:
        """
        完成真实回执插入后模拟最后存储失败。

        :param operation (SkillMigrationResolutionOperation): 本次完整回执
        """
        await save(self, operation)
        raise SkillContentError("HEAD_CHANGED", "injected final receipt failure")

    path = f"{BASE}/{identity}/resolve"
    payload = edit_request().model_dump(mode="json")
    with monkeypatch.context() as patch:
        patch.setattr(SkillMigrationResolutionRepository, "save_operation", reject)
        failed = await user_client.post(path, json=payload)
    assert failed.status_code == 409 and not failed.json()["committed"]
    assert await counts(stopped) == rows and await saved_plan(stopped, identity) == (0, [], 0)
    assert (await command(stopped, tmp_path)).expected == before
    retry = await user_client.post(path, json=payload)
    assert retry.status_code == 200 and retry.json()["status"] == "published"
