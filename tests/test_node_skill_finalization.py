"""
验证节点收尾 HTTP 流与准备下载权限相互独立。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import update
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_finalization import request
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.security.tokens import hash_token


async def stop(prepared: RuntimeHarness) -> None:
    """
    模拟正常退出报告与已结束的准备任务，上传不能依赖其旧租约。

    :param prepared (RuntimeHarness): 当前测试快照
    """
    async with prepared.database.begin() as session:
        await session.execute(
            update(Session).where(Session.id == prepared.session).values(status="stopped")
        )
        await session.execute(
            update(SessionSkillSnapshot)
            .where(SessionSkillSnapshot.id == prepared.snapshot)
            .values(status="started")
        )
        await session.execute(
            update(NodeTask).where(NodeTask.id == prepared.task).values(status="succeeded")
        )


async def test_node_streams_complete_input_and_replays_receipt(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    准备任务结束后独立收尾上传仍有效，完成响应只证明 persisted。

    :param node_client (AsyncClient): 已认证节点客户端
    :param prepared (RuntimeHarness): 固定快照身份
    """
    await stop(prepared)
    content = b"---\nname: learning\n---\nHTTP persisted"
    payload = request(prepared, content)
    begin_path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization"
    begin = await node_client.post(begin_path, json=payload.model_dump(mode="json"))
    assert begin.status_code == 200, begin.text
    plan = begin.json()["data"]
    assert datetime.fromisoformat(plan["expires_at"]).tzinfo == UTC
    base = f"/api/v1/node/skill-finalizations/{plan['id']}"
    params = {"upload_id": plan["upload_id"]}
    assert begin.json()["status"] == "upload_pending" and not begin.json()["committed"]
    wrong = await node_client.put(base + "/files/" + "a" * 64, params=params, content=b"unlisted")
    assert wrong.status_code == 409 and wrong.json()["errors"][0]["code"] == "CONTENT_NOT_DECLARED"
    incomplete = await node_client.post(base + "/complete", params=params)
    assert incomplete.status_code == 409
    uploaded = await node_client.put(
        base + "/files/" + payload.manifest.entries[1].sha256, params=params, content=content
    )
    assert uploaded.status_code == 200 and not uploaded.json()["committed"]
    complete = await node_client.post(base + "/complete", params=params)
    assert complete.status_code == 200 and complete.json()["committed"]
    assert complete.json()["status"] == "persisted"
    assert (await node_client.get(base)).json() == complete.json()
    assert (
        await node_client.post(begin_path, json=payload.model_dump(mode="json"))
    ).json() == complete.json()


async def test_bad_bytes_cannot_complete_or_publish(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    网络流摘要错误或超量时不产生完整收尾确认。

    :param node_client (AsyncClient): 已认证节点客户端
    :param prepared (RuntimeHarness): 固定快照身份
    """
    await stop(prepared)
    payload = request(prepared, b"expected")
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=payload.model_dump(mode="json"),
    )
    plan = response.json()["data"]
    base = f"/api/v1/node/skill-finalizations/{plan['id']}"
    for content in (b"modified", b"oversized content"):
        bad = await node_client.put(
            base + "/files/" + payload.manifest.entries[1].sha256,
            params={"upload_id": plan["upload_id"]},
            content=content,
        )
        assert bad.status_code == 422 and bad.json()["errors"][0]["code"] == "CONTENT_INVALID"
    status = await node_client.get(base)
    assert status.json()["status"] == "upload_pending" and not status.json()["committed"]


async def test_upload_cannot_borrow_other_receipts_or_override_owner(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    伪造用户字段和收尾 ID 不能扩展节点权限。

    :param node_client (AsyncClient): 已认证节点客户端
    :param prepared (RuntimeHarness): 固定快照身份
    """
    await stop(prepared)
    payload = request(prepared).model_dump(mode="json")
    payload["user_id"] = str(uuid4())
    invalid = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization", json=payload
    )
    assert invalid.status_code == 422
    assert (await node_client.get(f"/api/v1/node/skill-finalizations/{uuid4()}")).status_code == 404
    denied = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=request(prepared).model_dump(mode="json"),
        headers={"Authorization": "Bearer invalid-test-token"},
    )
    assert denied.status_code == 401


async def test_reserved_system_tree_is_never_saved(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    系统路径在读取字节前被拒绝，不混入普通账户状态。

    :param node_client (AsyncClient): 已认证节点客户端
    :param prepared (RuntimeHarness): 固定快照身份
    """
    await stop(prepared)
    payload = request(prepared).model_dump(mode="json")
    payload["manifest"] = {"entries": [{"path": "ego-browser", "kind": "directory"}]}
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization", json=payload
    )
    assert (
        response.status_code == 409
        and response.json()["errors"][0]["code"] == "SYSTEM_SKILL_IMMUTABLE"
    )


async def test_another_node_cannot_use_a_known_finalization_receipt(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    真实有效的其他节点令牌不能查询、写入或完成已知收尾与上传尝试。

    :param node_client (AsyncClient): 原始认证节点客户端
    :param prepared (RuntimeHarness): 原始精确快照
    """
    await stop(prepared)
    payload = request(prepared)
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=payload.model_dump(mode="json"),
    )
    plan = response.json()["data"]
    token = "other-finalization-node:" + str(uuid4())
    async with prepared.database.begin() as session:
        session.add(
            Node(
                name="其他上传节点",
                status="healthy",
                region_code="global",
                node_token_hash=hash_token("skill-node-test", token),
            )
        )
    headers = {"Authorization": f"Bearer {token}"}
    base = f"/api/v1/node/skill-finalizations/{plan['id']}"
    params = {"upload_id": plan["upload_id"]}
    status = await node_client.get(base, headers=headers)
    upload = await node_client.put(
        base + "/files/" + payload.manifest.entries[1].sha256,
        params=params,
        headers=headers,
        content=b"unauthorized",
    )
    complete = await node_client.post(base + "/complete", params=params, headers=headers)
    publication = await node_client.post(base + "/publish", headers=headers)
    reclamation = await node_client.get(
        base + "/reclamation-authorization", headers=headers, params={"request_id": str(uuid4())}
    )
    for result in (status, upload, complete, publication, reclamation):
        assert (
            result.status_code == 404
            and result.json()["errors"][0]["code"] == "FINALIZATION_NOT_FOUND"
        )


@pytest.mark.parametrize("unclean", [False, True])
async def test_node_publication_requires_persistence_and_reports_atomic_result(
    node_client: AsyncClient,
    prepared: RuntimeHarness,
    unclean: bool,
) -> None:
    """
    节点先持久化再发布，异常退出只归档，重试不改变第一次结果。

    :param node_client (AsyncClient): 已认证节点客户端
    :param prepared (RuntimeHarness): 原始快照身份
    :param unclean (bool): 原始终止分类
    """
    await stop(prepared)
    content = b"updated instructions"
    payload = request(prepared, content, unclean=unclean)
    begin = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=payload.model_dump(mode="json"),
    )
    plan = begin.json()["data"]
    base = f"/api/v1/node/skill-finalizations/{plan['id']}"
    pending = await node_client.post(base + "/publish")
    assert pending.status_code == 409 and pending.json()["errors"][0]["code"] == "STATE_PENDING"
    params = {"upload_id": plan["upload_id"]}
    assert (
        await node_client.put(
            base + "/files/" + payload.manifest.entries[1].sha256, params=params, content=content
        )
    ).status_code == 200
    assert (await node_client.post(base + "/complete", params=params)).status_code == 200
    result = await node_client.post(base + "/publish")
    assert result.status_code == 200, result.text
    outcome = "detached" if unclean else "published"
    assert result.json()["status"] == outcome and result.json()["committed"]
    assert result.json()["data"]["reason"] == ("unclean" if unclean else None)
    assert (await node_client.post(base + "/publish")).json() == result.json()
    assert (await node_client.get(base)).json()["status"] == outcome
