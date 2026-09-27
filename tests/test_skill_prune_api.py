"""
验证公开 prune 的真实认证、功能开关、完整分页和原键恢复路由。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.api.deps import get_settings
from agent_remote_server.config import Settings

_PATH = "/api/v1/skills/state/prune"


async def test_http_prune_pagination_command_and_independent_queries(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    真实请求完整遍历后受理，原键与身份返回相同结果，详情和进度各自可恢复。

    :param user_client (AsyncClient): 真实用户认证客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    """
    await shared_directory(stopped, tmp_path)
    payload = {
        "selector": {"account_id": str(stopped.account), "skill": "learning"},
        "all_unreferenced": True,
        "limit": 3,
    }
    offset = 0
    while True:
        response = await user_client.post(_PATH + "/preview", json=payload)
        assert response.status_code == 200, response.text
        assert not response.json()["committed"]
        page = response.json()["data"]
        assert page["offset"] == offset
        offset += len(page["rows"])
        if page["next_cursor"] is None:
            break
        assert page["confirmation"] is None
        cursor_command = await user_client.post(
            _PATH,
            json={
                "idempotency_key": "cursor-not-confirmation",
                "confirmation": page["next_cursor"],
            },
        )
        assert cursor_command.status_code == 422
        payload.update(selector=page["summary"]["binding"]["selector"], cursor=page["next_cursor"])
    assert offset == page["total"] and page["confirmation"]
    request = {"idempotency_key": str(uuid4()), "confirmation": page["confirmation"]}
    accepted = await user_client.post(_PATH, json=request)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["committed"] and accepted.json()["status"] == "accepted"
    assert (await user_client.post(_PATH, json=request)).json() == accepted.json()
    assert (
        await user_client.get(_PATH + "/operations", params={"key": request["idempotency_key"]})
    ).json() == accepted.json()
    operation = _PATH + "/operations/" + accepted.json()["operation_id"]
    assert (await user_client.get(operation)).json() == accepted.json()
    details = (await user_client.get(operation + "/entries", params={"limit": 2})).json()["data"]
    assert details["offset"] == 0 and details["total"] == page["total"]
    assert len(details["rows"]) == 2 and details["next_offset"] == 2
    progress = (await user_client.get(operation + "/progress")).json()["data"]
    assert progress["pending_tasks"] > 0 and progress["completed_tasks"] == 0
    assert (await user_client.get(operation + "/entries", params={"limit": 101})).status_code == 422
    assert (await user_client.get(_PATH + f"/operations/{uuid4()}")).status_code == 404
    request["confirmation"] += "changed"
    conflict = await user_client.post(_PATH, json=request)
    assert (
        conflict.status_code == 409
        and conflict.json()["errors"][0]["code"] == "IDEMPOTENCY_CONFLICT"
    )


@pytest.mark.parametrize("kind", ["device", "node", "other-user", "disabled"])
async def test_prune_routes_require_live_owner_and_feature_gate(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    kind: str,
) -> None:
    """
    全部读写入口使用原有身份和开关，已知原键也不能绕过授权。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param kind (str): 需要拒绝的权限或开关状态
    """
    await shared_directory(stopped, tmp_path)
    payload = {
        "selector": {"account_id": str(stopped.account), "scope": "account-directory"},
        "all_unreferenced": True,
    }
    last = (await user_client.post(_PATH + "/preview", json=payload)).json()["data"]
    assert last["next_cursor"] is None
    request = {"idempotency_key": str(uuid4()), "confirmation": last["confirmation"]}
    accepted = await user_client.post(_PATH, json=request)
    assert accepted.status_code == 200, accepted.text
    operation = _PATH + "/operations/" + accepted.json()["operation_id"]
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
    expected = {"device": 403, "node": 401, "other-user": 404, "disabled": 503}[kind]
    response = await user_client.post(_PATH + "/preview", json=payload, headers=headers)
    assert response.status_code == expected
    for path in (operation, operation + "/entries", operation + "/progress"):
        assert (await user_client.get(path, headers=headers)).status_code == expected
    response = await user_client.post(_PATH, json=request, headers=headers)
    assert response.status_code == (422 if kind == "other-user" else expected)
