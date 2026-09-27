"""
验证独立版本准备的用户认证、冲突提交和原始回执查询。
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, publish
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_preparation import request, update_version
from test_skill_snapshots import prepared as prepared


async def test_http_conflict_commits_and_preview_does_not_create_receipt(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    冲突是持久化受理结果，既不回滚输入也不误称完整目标已发布。

    :param user_client (AsyncClient): 真实认证客户端
    :param stopped (RuntimeHarness): 旧版会话
    :param tmp_path (Path): 内容卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/SKILL.md": b"changed locally"}),
    )
    await update_version(stopped, tmp_path, "changed upstream")
    payload = (await request(stopped, tmp_path)).model_dump(mode="json")
    payload["dry_run"] = True
    preview = await user_client.post("/api/v1/skills/state/prepare", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "conflicted" and not preview.json()["committed"]
    assert preview.json()["operation_id"] is None
    query = {"key": payload["idempotency_key"]}
    assert (
        await user_client.get("/api/v1/skills/state/preparations", params=query)
    ).status_code == 404
    payload["dry_run"] = False
    result = await user_client.post("/api/v1/skills/state/prepare", json=payload)
    assert result.status_code == 200 and result.json()["committed"], result.text
    assert result.json()["data"]["result_checkpoint_id"] is None
    assert result.json()["data"]["source_checkpoint_id"] is not None
    receipt = await user_client.get("/api/v1/skills/state/preparations", params=query)
    assert receipt.json()["data"]["result"] == result.json()["data"]
    assert (
        await user_client.post("/api/v1/skills/state/prepare", json=payload)
    ).json() == result.json()
    other = await user(stopped.database)
    headers = {"Authorization": "Bearer " + await token(stopped, other)}
    assert (
        await user_client.get("/api/v1/skills/state/preparations", params=query, headers=headers)
    ).status_code == 404


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_http_preparation_rejects_nonowner_authorities(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    持有完整前置条件也不能使节点、设备或其他用户获得迁移权限。

    :param user_client (AsyncClient): 真实认证客户端
    :param stopped (RuntimeHarness): 原始用户账户
    :param tmp_path (Path): 私有内容卷
    :param kind (str): 未授权凭据种类
    """
    payload = await request(stopped, tmp_path)
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    headers = {
        "Authorization": "Bearer "
        + await token(stopped, owner, "user" if kind == "other-user" else kind)
    }
    response = await user_client.post(
        "/api/v1/skills/state/prepare", headers=headers, json=payload.model_dump(mode="json")
    )
    assert response.status_code == (
        404 if kind == "other-user" else 403 if kind == "device" else 401
    )
