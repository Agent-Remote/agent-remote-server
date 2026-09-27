"""
验证状态命令真实用户认证、当前选版、预览确认和不可变回执。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command, execute

from agent_remote_server.schemas.skill_library import (
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)


async def test_http_preview_commit_replay_and_operation_lookup(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    实际接口从当前状态预览到条件提交，查询键始终返回原始已接受结果。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"private"})
    )
    params = {"account_id": str(stopped.account), "skill": "learning"}
    current = (await user_client.get("/api/v1/skills/state/current", params=params)).json()["data"]
    diff = (await user_client.get("/api/v1/skills/state/diff", params=params)).json()["data"]
    assert [item["path"] for item in diff["items"]] == ["learning/memory"]
    request = {
        "idempotency_key": "http-reset",
        "action": "reset",
        "selector": current["selector"],
        "expected": current["precondition"],
        "dry_run": True,
    }
    preview = await user_client.post("/api/v1/skills/state/commands", json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "preview" and not preview.json()["committed"]
    assert preview.json()["data"]["branch_changes"][0]["changes"][0]["path"] == "learning/memory"
    assert (await user_client.get("/api/v1/skills/state/current", params=params)).json()[
        "data"
    ] == current
    assert (
        await user_client.get("/api/v1/skills/state/operations", params={"key": "http-reset"})
    ).status_code == 404
    request["dry_run"] = False
    result = await user_client.post("/api/v1/skills/state/commands", json=request)
    assert result.status_code == 200 and result.json()["committed"], result.text
    assert (await user_client.get("/api/v1/skills/state/diff", params=params)).json()["data"][
        "items"
    ] == []
    assert (
        await user_client.post("/api/v1/skills/state/commands", json=request)
    ).json() == result.json()
    assert (
        await user_client.get("/api/v1/skills/state/operations", params={"key": "http-reset"})
    ).json() == result.json()
    operation_path = "/api/v1/skills/state/operations/" + result.json()["operation_id"]
    assert (await user_client.get(operation_path)).json() == result.json()
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert (await user_client.get(operation_path)).json() == result.json()
    assert (await user_client.get(f"/api/v1/skills/state/operations/{uuid4()}")).status_code == 404
    request["idempotency_key"] = "stale-reset"
    stale = await user_client.post("/api/v1/skills/state/commands", json=request)
    assert (
        stale.status_code == 409
        and stale.json()["errors"][0]["code"] == "STATE_PRECONDITION_CHANGED"
    )


async def test_pin_selects_old_branch_and_preview_lists_its_own_data_loss(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    目录显示新版时，固定旧版重置仍必须列出旧分支自身将被清空的学习文件。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"old learned"})
    )
    old = (await command(stopped, tmp_path)).expected.targets[0]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    await execute(stopped, tmp_path, await command(stopped, tmp_path))
    await library.execute(
        SkillRuleRequest(
            command="pin",
            skill="learning",
            revision=str(old.revision_id),
            scope=SkillScope(account_id=stopped.account),
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    params = {"account_id": str(stopped.account), "skill": "learning"}
    selected = (await user_client.get("/api/v1/skills/state/current", params=params)).json()["data"]
    target = selected["precondition"]["targets"][0]
    assert (
        target["state_id"] == str(old.state_id) and target["rule"]["revision_source"] == "account"
    )
    diff = (await user_client.get("/api/v1/skills/state/diff", params=params)).json()["data"]
    assert diff["checkpoint_id"] == str(old.head_checkpoint_id)
    request = await command(stopped, tmp_path, dry_run=True)
    preview = await execute(stopped, tmp_path, request)
    assert "learning/memory" not in {change.path for change in preview.changes}
    changes = preview.branch_changes[0].changes
    assert changes is not None
    assert any(change.path == "learning/memory" and change.current is None for change in changes)


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_state_commands_reject_nonowner_authorities(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    设备、节点和其他用户不能通过提供原前置条件获得状态修改权限。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 未获授权的凭据种类
    """
    accepted = await execute(stopped, tmp_path, await command(stopped, tmp_path))
    request = await command(stopped, tmp_path)
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    value = await token(stopped, owner, "user" if kind == "other-user" else kind)
    headers = {"Authorization": "Bearer " + value}
    response = await user_client.post(
        "/api/v1/skills/state/commands", headers=headers, json=request.model_dump(mode="json")
    )
    assert response.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )
    response = await user_client.get(
        "/api/v1/skills/state/current",
        headers=headers,
        params={"account_id": str(stopped.account), "scope": "account-directory"},
    )
    assert response.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )
    receipt = await user_client.get(
        f"/api/v1/skills/state/operations/{accepted.operation_id}", headers=headers
    )
    assert receipt.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )
    assert (await command(stopped, tmp_path)).expected == request.expected
