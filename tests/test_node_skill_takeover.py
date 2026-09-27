"""
验证真实节点认证下的接管清单、流式上传及完整权威回执。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from skill_takeover_support import TakeoverHarness, tree, writer_task
from skill_takeover_support import takeover as takeover
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.api import node_skill_takeover
from agent_remote_server.api.deps import get_session
from agent_remote_server.main import create_app
from agent_remote_server.models import Node, NodeTask, User
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.security.tokens import hash_token


@dataclass
class TakeoverHTTP:
    """
    保留真实认证客户端和已出租的原始预约。
    """

    client: AsyncClient
    receipt: SkillAccountTakeover
    path: str
    params: dict[str, str]


@pytest.fixture
async def takeover_http(takeover: TakeoverHarness) -> AsyncIterator[TakeoverHTTP]:
    """
    每个请求使用新事务，认证器不被测试替换。

    :param takeover (TakeoverHarness): 旧账户与内容卷
    :return AsyncIterator[TakeoverHTTP]: 节点 HTTP 入口
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    token = f"test-takeover-node:{takeover.node}"
    async with takeover.library.database.begin() as session:
        node = await session.get(Node, takeover.node)
        assert node is not None
        node.node_token_hash = hash_token(takeover.settings.secret_key, token)
    app = create_app(takeover.settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        失败请求离开时自动回滚未提交修改。

        :return AsyncIterator[AsyncSession]: 请求独立事务
        """
        async with takeover.library.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield TakeoverHTTP(
            client,
            receipt,
            f"/api/v1/node/skill-takeovers/{receipt.id}",
            {"task_id": str(receipt.task_id)},
        )
    await app.state.database_engine.dispose()


async def test_takeover_http_streams_complete_tree_and_replays_original_receipt(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    响应丢失后重试完整树仍返回首次检查点，终态任务可读取原收据。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 私有内容卷和数据库
    """
    http = takeover_http
    grant = await http.client.get(http.path, params=http.params)
    assert grant.status_code == 200, grant.text
    assert grant.json()["data"]["inventory"] == []
    assert grant.json()["committed"] is False
    files = {
        "manual/SKILL.md": b"---\nname: manual\ndescription: Test\n---\nInstruction\n",
        "manual/state.bin": b"\x00\xffstate",
        "notes.txt": b"notes",
    }
    manifest = tree(files)
    capture = takeover.capture(http.receipt, manifest).model_dump(mode="json")
    started = await http.client.post(http.path + "/capture", params=http.params, json=capture)
    assert started.status_code == 200, started.text
    upload = started.json()["data"]["upload_id"]
    params = http.params | {"upload_id": upload}
    missing = await http.client.post(http.path + "/complete", params=params)
    assert missing.status_code == 409
    assert missing.json()["errors"][0]["code"] == "CONTENT_INCOMPLETE"
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        for _ in range(2):
            response = await http.client.put(
                http.path + "/files/" + entry.sha256, params=params, content=files[entry.path]
            )
            assert response.status_code == 200, response.text
            assert response.json()["committed"] is False
    complete = await http.client.post(http.path + "/complete", params=params)
    assert complete.status_code == 200, complete.text
    result = complete.json()
    assert result["committed"] is True and result["status"] == "committed"
    assert result["data"]["checkpoint_id"] is not None
    assert "Instruction" not in complete.text and "notes.txt" not in complete.text
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None
        task.status, task.lease_until = "succeeded", None
    for response in (
        await http.client.get(http.path, params=http.params),
        await http.client.post(http.path + "/complete", params=params),
        await http.client.post(http.path + "/capture", params=http.params, json=capture),
    ):
        assert response.status_code == 200 and response.json() == result
    denied = await http.client.put(
        http.path + "/files/" + manifest.entries[-1].sha256, params=params, content=b"notes"
    )
    assert denied.status_code == 409


@pytest.mark.parametrize("route", ["status", "capture", "file", "complete"])
@pytest.mark.parametrize("condition", ["task", "lease", "terminal", "payload", "owner", "node"])
async def test_takeover_http_rechecks_each_request(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness, route: str, condition: str
) -> None:
    """
    已取得的授权不能跨任务、节点、租约或用户撤销边界复用。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 数据库与所有者
    :param route (str): 请求阶段
    :param condition (str): 撤销或身份替换条件
    """
    http = takeover_http
    params = dict(http.params)
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None
        if condition == "task":
            params["task_id"] = str(uuid4())
        elif condition == "lease":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif condition == "terminal":
            task.status = "cancelled"
        elif condition == "payload":
            task.payload = task.payload | {"protocol_version": True}
            flag_modified(task, "payload")
        elif condition == "owner":
            user = await session.get(User, takeover.library.owner)
            assert user is not None
            user.status = "disabled"
        else:
            token = f"test-other-node:{uuid4()}"
            session.add(
                Node(
                    name="其他节点",
                    status="healthy",
                    region_code="global",
                    node_token_hash=hash_token(takeover.settings.secret_key, token),
                )
            )
            http.client.headers["Authorization"] = f"Bearer {token}"
    if route == "status":
        response = await http.client.get(http.path, params=params)
    elif route == "capture":
        response = await http.client.post(http.path + "/capture", params=params, content=b"invalid")
    elif route == "file":
        response = await http.client.put(
            http.path + "/files/" + "a" * 64,
            params=params | {"upload_id": str(uuid4())},
            content=b"invalid",
        )
    else:
        response = await http.client.post(
            http.path + "/complete", params=params | {"upload_id": str(uuid4())}
        )
    assert response.status_code == 404, response.text
    assert response.json()["errors"][0]["code"] == "TAKEOVER_NOT_FOUND"


async def test_takeover_status_permits_fencing_before_natural_drain(
    takeover: TakeoverHarness,
) -> None:
    """
    固定清单可在旧任务结束前读取，但捕获仍必须等候排空。

    :param takeover (TakeoverHarness): 未预约账户
    """
    await writer_task(takeover, status="running")
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    async with takeover.library.database.begin() as session:
        fetched = await takeover.service(session).get(takeover.node, receipt.id, receipt.task_id)
        assert len(fetched.inventory_json) == 1 and fetched.status == "reserved"
    from agent_remote_server.services.skills.content import SkillContentError

    with pytest.raises(SkillContentError, match="remain active"):
        await takeover.begin(receipt, takeover.capture(receipt, tree({})))


async def test_takeover_capture_body_limits_and_strict_declaration(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    畸形静止声明与超限正文不能建立上传尝试。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 原始预约
    :param monkeypatch (pytest.MonkeyPatch): 有界传输阈值替换器
    """
    http = takeover_http
    capture = takeover.capture(http.receipt, tree({})).model_dump(mode="json")
    capture["writers_quiescent"] = 1
    rejected = await http.client.post(http.path + "/capture", params=http.params, json=capture)
    assert rejected.status_code == 422
    monkeypatch.setattr(node_skill_takeover, "_MAX_CAPTURE_BYTES", 16)
    oversized = await http.client.post(
        http.path + "/capture", params=http.params, content=b"x" * 17
    )
    assert oversized.status_code == 413
    state = await http.client.get(http.path, params=http.params)
    assert state.json()["data"]["upload_attempt"] == 0


async def test_takeover_file_integrity_and_current_upload_scope(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    篡改字节和非当前上传不落库，随后正确重传才允许提交。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 原始预约
    """
    http = takeover_http
    manifest = tree({"state": b"original"})
    capture = takeover.capture(http.receipt, manifest).model_dump(mode="json")
    response = await http.client.post(http.path + "/capture", params=http.params, json=capture)
    params = http.params | {"upload_id": response.json()["data"]["upload_id"]}
    path = http.path + "/files/" + manifest.entries[0].sha256
    for data in (b"modified", b"short", b"original-extra"):
        rejected = await http.client.put(path, params=params, content=data)
        assert rejected.status_code == 422
    wrong = await http.client.put(
        path, params=http.params | {"upload_id": str(uuid4())}, content=b"original"
    )
    assert wrong.status_code == 409 and wrong.json()["errors"][0]["code"] == "UPLOAD_SUPERSEDED"
    missing = await http.client.post(http.path + "/complete", params=params)
    assert missing.status_code == 409
    assert missing.json()["errors"][0]["code"] == "CONTENT_INCOMPLETE"
    assert (await http.client.put(path, params=params, content=b"original")).status_code == 200
    assert (await http.client.post(http.path + "/complete", params=params)).json()[
        "committed"
    ] is True


async def test_takeover_feature_gate_and_real_credentials(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    节点令牌与功能开关均在正文处理前生效。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 应用设置
    """
    http = takeover_http
    takeover.settings.skill_manager_enabled = False
    disabled = await http.client.post(
        http.path + "/capture", params=http.params, content=b"invalid"
    )
    assert disabled.status_code == 503
    takeover.settings.skill_manager_enabled = True
    http.client.headers.clear()
    assert (await http.client.get(http.path, params=http.params)).status_code == 401
    assert (
        await http.client.get(
            http.path, params=http.params, headers={"Authorization": "Bearer invalid"}
        )
    ).status_code == 401


async def test_takeover_file_reauthorizes_after_receiving_stream(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    上传期间租约到期必须在文件落库前拒绝，旧授权不能覆盖网络等待。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 原始预约
    :param monkeypatch (pytest.MonkeyPatch): 确定性的时间替换器
    """
    from unittest.mock import Mock

    from agent_remote_server.services.skills import takeover_context

    http = takeover_http
    manifest = tree({"state": b"retained"})
    capture = takeover.capture(http.receipt, manifest).model_dump(mode="json")
    begun = await http.client.post(http.path + "/capture", params=http.params, json=capture)
    params = http.params | {"upload_id": begun.json()["data"]["upload_id"]}
    future = Mock(wraps=datetime)
    future.now.return_value = datetime.now(UTC) + timedelta(minutes=10)

    async def delayed_bytes() -> AsyncIterator[bytes]:
        """
        只有正文开始读取后才模拟租约到期。

        :return AsyncIterator[bytes]: 完整但发送结束时过期的字节
        """
        yield b"retained"
        monkeypatch.setattr(takeover_context, "datetime", future)

    response = await http.client.put(
        http.path + "/files/" + manifest.entries[0].sha256, params=params, content=delayed_bytes()
    )
    assert (
        response.status_code == 404 and response.json()["errors"][0]["code"] == "TAKEOVER_NOT_FOUND"
    )
    monkeypatch.undo()
    missing = await http.client.post(http.path + "/complete", params=params)
    assert (
        missing.status_code == 409 and missing.json()["errors"][0]["code"] == "CONTENT_INCOMPLETE"
    )


async def test_polled_takeover_exposes_exact_record_identity(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    轮询同时给出逻辑幂等身份和真实授权 UUID，不要求节点猜测内部主键。

    :param takeover_http (TakeoverHTTP): 已认证入口
    :param takeover (TakeoverHarness): 已预约数据库任务
    """
    http = takeover_http
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None
        task.status, task.lease_until = "pending", None
    polled = await http.client.post("/api/v1/node-api/tasks/poll")
    assert polled.status_code == 200, polled.text
    tasks = polled.json()["data"]["tasks"]
    task = next(item for item in tasks if item["task_type"] == "takeover_tool_account_skills")
    assert task["task_id"] == f"takeover_tool_account_skills:{http.receipt.id}"
    assert task["task_record_id"] == str(http.receipt.task_id)
    authorized = await http.client.get(http.path, params={"task_id": task["task_record_id"]})
    assert authorized.status_code == 200, authorized.text
    rejected = await http.client.get(http.path, params={"task_id": task["task_id"]})
    assert rejected.status_code == 422
