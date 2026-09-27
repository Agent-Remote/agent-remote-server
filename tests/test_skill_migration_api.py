"""
验证用户接口的明确增量基线、不可变回执和跨身份拒绝。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_migration import request, versions
from test_skill_preparation import prepare
from test_skill_preparation import request as preparation_request
from test_skill_snapshots import prepared as prepared


async def test_http_incremental_preview_commit_receipt_and_sequence(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    HTTP 查询返回精确 from/to 与基线，成功后再次查询显示已迁入 checkpoint。

    :param user_client (AsyncClient): 用户认证客户端
    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 私有内容卷
    """
    source, target = await versions(stopped, tmp_path)
    params = {
        "account_id": str(stopped.account),
        "skill": "learning",
        "from_revision": "r1",
        "to_revision": "r2",
    }
    current = await user_client.get("/api/v1/skills/state/migration/current", params=params)
    assert current.status_code == 200, current.text
    assert current.json()["data"]["source"]["revision_id"] == str(source)
    assert current.json()["data"]["target"]["revision_id"] == str(target)
    payload = {
        "selector": params,
        "expected": current.json()["data"],
        "idempotency_key": "migration-http",
        "dry_run": True,
    }
    preview = await user_client.post("/api/v1/skills/state/migrate", json=payload)
    assert preview.status_code == 200 and not preview.json()["committed"], preview.text
    assert preview.json()["data"]["base_source"] == "old_original"
    assert preview.json()["data"]["current_source"] == "target_original"
    assert preview.json()["data"]["incoming_source"] == "source_published"
    query = {"key": "migration-http"}
    assert (
        await user_client.get("/api/v1/skills/state/migration/operations", params=query)
    ).status_code == 404
    payload["dry_run"] = False
    result = await user_client.post("/api/v1/skills/state/migrate", json=payload)
    assert result.status_code == 200 and result.json()["committed"], result.text
    assert result.json()["data"]["migration_sequence"] == 1
    assert (
        await user_client.post("/api/v1/skills/state/migrate", json=payload)
    ).json() == result.json()
    receipt = await user_client.get("/api/v1/skills/state/migration/operations", params=query)
    assert receipt.json()["data"]["result"] == result.json()["data"]
    operation_url = f"/api/v1/skills/state/migration/operations/{result.json()['operation_id']}"
    assert (await user_client.get(operation_url)).json() == receipt.json()
    missing = await user_client.get(f"/api/v1/skills/state/migration/operations/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["errors"][0]["code"] == "OPERATION_NOT_FOUND"
    assert (await user_client.get("/api/v1/skills/state/preparations", params=query)).json()[
        "errors"
    ][0]["code"] == "OPERATION_KIND_MISMATCH"
    latest = (
        await user_client.get("/api/v1/skills/state/migration/current", params=params)
    ).json()["data"]
    assert latest["last_sequence"] == 1 and not latest["source_has_unmigrated_checkpoint"]
    assert (
        latest["last_migrated_checkpoint_id"] == current.json()["data"]["source"]["checkpoint_id"]
    )
    other = await user(stopped.database)
    headers = {"Authorization": "Bearer " + await token(stopped, other)}
    assert (
        await user_client.get(
            "/api/v1/skills/state/migration/operations", params=query, headers=headers
        )
    ).status_code == 404
    assert (await user_client.get(operation_url, headers=headers)).status_code == 404


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_http_incremental_migration_rejects_other_authorities(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    提供完整请求和版本身份不能扩大其他凭据的账户权限。

    :param user_client (AsyncClient): 真实认证客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    :param kind (str): 未授权凭据种类
    """
    source, target = await versions(stopped, tmp_path)
    payload = await request(stopped, tmp_path, source, target)
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    headers = {
        "Authorization": "Bearer "
        + await token(stopped, owner, "user" if kind == "other-user" else kind)
    }
    response = await user_client.post(
        "/api/v1/skills/state/migrate", json=payload.model_dump(mode="json"), headers=headers
    )
    assert response.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )
    response = await user_client.get(
        "/api/v1/skills/state/migration/current",
        params=payload.selector.model_dump(mode="json"),
        headers=headers,
    )
    assert response.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )

    accepted = await user_client.post(
        "/api/v1/skills/state/migrate", json=payload.model_dump(mode="json")
    )
    assert accepted.status_code == 200
    operation_id = accepted.json()["operation_id"]
    response = await user_client.get(
        f"/api/v1/skills/state/migration/operations/{operation_id}", headers=headers
    )
    assert response.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )


async def test_migration_id_rejects_first_use_preparation(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    共用账本不能使首次准备回执被增量迁移入口重新解释。

    :param user_client (AsyncClient): 所有者客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有内容卷
    """
    await versions(stopped, tmp_path)
    payload = await preparation_request(stopped, tmp_path)
    result = await prepare(stopped, tmp_path, payload)
    for url in (
        f"/api/v1/skills/state/migration/operations/{result.operation_id}",
        f"/api/v1/skills/state/migration/operations?key={payload.idempotency_key}",
    ):
        response = await user_client.get(url)
        assert response.status_code == 409
        assert response.json()["errors"][0]["code"] == "OPERATION_KIND_MISMATCH"
