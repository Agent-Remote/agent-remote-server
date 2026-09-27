"""
验证独立节点回收核验路由不能把上传或历史回执当成完整远端副本。
"""

from pathlib import Path
from uuid import uuid4

from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from test_node_skill_content import node_client as node_client
from test_node_skill_finalization import stop
from test_skill_content_service import database as database
from test_skill_finalization import request
from test_skill_snapshots import prepared as prepared


async def test_node_reclamation_http_requires_terminal_verified_content(
    node_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    通过真实上传和发布取得短期完整性授权，损坏内容后立即拒绝新授权。

    :param node_client (AsyncClient): 已认证原节点客户端
    :param prepared (RuntimeHarness): 原始固定快照
    :param tmp_path (Path): 私有内容卷
    """
    await stop(prepared)
    content = b"---\nname: learning\n---\nreclaimable"
    payload = request(prepared, content)
    begin = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=payload.model_dump(mode="json"),
    )
    assert begin.status_code == 200, begin.text
    plan = begin.json()["data"]
    base = f"/api/v1/node/skill-finalizations/{plan['id']}"
    challenge = uuid4()
    authority = base + "/reclamation-authorization?request_id=" + str(challenge)
    pending = await node_client.get(authority)
    assert pending.status_code == 409, pending.text
    params = {"upload_id": plan["upload_id"]}
    digest = payload.manifest.entries[1].sha256
    assert (
        await node_client.put(base + "/files/" + digest, params=params, content=content)
    ).status_code == 200
    assert (await node_client.post(base + "/complete", params=params)).status_code == 200
    assert (await node_client.get(authority)).status_code == 409
    published = await node_client.post(base + "/publish")
    assert published.status_code == 200, published.text
    before = (await node_client.get(base)).json()
    authorized = await node_client.get(authority)
    assert authorized.status_code == 200, authorized.text
    body = authorized.json()
    assert body["status"] == "reclaimable" and body["committed"] and not body["retryable"]
    assert body["data"]["node_id"] == str(prepared.node)
    assert body["data"]["request_id"] == str(challenge)
    assert body["data"]["session_id"] == str(prepared.session)
    assert body["data"]["checkpoint_id"] == before["data"]["checkpoint_id"]
    assert body["data"]["publication_id"] == published.json()["data"]["id"]
    assert (await node_client.get(base)).json() == before
    path = tmp_path / "objects" / str(prepared.owner) / digest[:2] / digest
    path.chmod(0o600)
    path.write_bytes(b"x" * len(content))
    path.chmod(0o400)
    denied = await node_client.get(authority)
    assert denied.status_code == 409, denied.text
    assert denied.json()["errors"][0]["code"] == "STATE_RECLAMATION_UNAVAILABLE"
    assert (await node_client.get(base)).json() == before
    unauthorized = await node_client.get(
        authority, headers={"Authorization": "Bearer invalid-test-token"}
    )
    assert unauthorized.status_code == 401
