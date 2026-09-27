"""
验证真实用户 HTTP 协议中的本地详情、账户规则与同名拒绝。
"""

from pathlib import Path
from uuid import uuid4

from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_local import activate, register, source
from test_skill_snapshots import prepared as prepared


async def test_local_http_query_rule_replay_and_name_conflict(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    使用真实用户令牌读取本地来源，规则提交后仍可精确重放原结果。

    :param user_client (AsyncClient): 已登录 HTTP 客户端
    :param stopped (RuntimeHarness): 受管账户
    :param tmp_path (Path): 内容卷
    """
    item = await register(stopped, tmp_path, await source(stopped, tmp_path))
    await activate(stopped, item)
    params = {"account_id": str(stopped.account)}
    info = await user_client.get("/api/v1/skills/installations/notes", params=params)
    assert info.status_code == 200, info.text
    assert info.json()["data"]["origin"] == "account_local"
    assert info.json()["data"]["id"] == str(item.id)
    listing = await user_client.get("/api/v1/skills", params={**params, "effective": "true"})
    assert listing.status_code == 200
    data = listing.json()["data"]
    detail = info.json()["data"]
    assert detail["storage"]["scope"] == "user"
    assert all(row["retention"]["kind"] == "local_revision" for row in detail["revisions"])
    assert data["local_items"] == [
        {
            **detail,
            "storage": None,
            "revisions": [{**row, "retention": None} for row in detail["revisions"]],
        }
    ]
    request = dict(
        command="disable",
        skill="notes",
        scope=params,
        expected_generation=data["generation"],
        idempotency_key=str(uuid4()),
    )
    result = await user_client.post("/api/v1/skills/rules", json=request)
    assert result.status_code == 200 and result.json()["committed"], result.text
    assert result.json()["data"]["skill_ids"] == [str(item.id)]
    assert [target["account_id"] for target in result.json()["data"]["targets"]] == [
        str(stopped.account)
    ]
    assert (await user_client.post("/api/v1/skills/rules", json=request)).json() == result.json()
    disabled = (
        await user_client.get("/api/v1/skills", params={**params, "effective": "true"})
    ).json()["data"]["local_items"]
    assert len(disabled) == 1 and not disabled[0]["enabled"]
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate("notes"))
    conflict = await user_client.get("/api/v1/skills/installations/notes", params=params)
    assert conflict.status_code == 409
    assert conflict.json()["errors"][0]["code"] == "SKILL_SOURCE_CONFLICT"
    exact = await user_client.get(f"/api/v1/skills/installations/{item.id}", params=params)
    assert exact.status_code == 200 and exact.json()["data"]["origin"] == "account_local"
