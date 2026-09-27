"""
验证接管任务续租的领取轮次、活动租约、并发及网络等待边界。
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from test_node_skill_takeover import TakeoverHTTP
from test_node_skill_takeover import takeover_http as takeover_http
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models import NodeTask, User
from agent_remote_server.schemas.skill_takeover import SkillTakeoverLeaseRequest


async def prepare_attempt(
    takeover: TakeoverHarness, http: TakeoverHTTP, lease_seconds: int = 5
) -> None:
    """
    固定真实轮询会增加的领取轮次，并为并行 CI 的调度等待保留有效租约。

    :param takeover (TakeoverHarness): 数据库
    :param http (TakeoverHTTP): 原始预约
    :param lease_seconds (int): 初始租约时长，网络同步用例允许额外调度时间
    """
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None
        task.retry_count = 3
        task.lease_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)


async def test_takeover_renewal_preserves_attempt_and_extends_only_live_lease(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    续租只更新同一任务的截止时间，不创建新轮次或捕获。

    :param takeover_http (TakeoverHTTP): 认证入口
    :param takeover (TakeoverHarness): 数据库
    """
    http = takeover_http
    await prepare_attempt(takeover, http)
    for _ in range(2):
        response = await http.client.post(
            http.path + "/lease", params=http.params, json={"lease_attempt": 3}
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["task_id"] == str(http.receipt.task_id) and data["takeover_id"] == str(
            http.receipt.id
        )
        assert data["node_id"] == str(takeover.node) and data["lease_attempt"] == 3
        now = datetime.fromisoformat(data["server_time"])
        until = datetime.fromisoformat(data["lease_until"])
        assert (until - now).total_seconds() == takeover.settings.node_task_lease_seconds
        assert 0 < data["renew_after_milliseconds"] < (until - now).total_seconds() * 1000
    async with takeover.library.database() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None and task.retry_count == 3 and task.status == "running"


@pytest.mark.parametrize(
    "change", ["attempt", "expired", "pending", "terminal", "owner", "task", "committed"]
)
async def test_takeover_renewal_cannot_revive_or_borrow_authority(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness, change: str
) -> None:
    """
    旧轮次、过期状态、撤销用户与原始提交不能重新获得可写租约。

    :param takeover_http (TakeoverHTTP): 认证入口
    :param takeover (TakeoverHarness): 数据库
    :param change (str): 失效条件
    """
    http = takeover_http
    await prepare_attempt(takeover, http)
    if change == "committed":
        receipt = await takeover.begin(http.receipt, takeover.capture(http.receipt, tree({})))
        await takeover.complete(receipt)
    params = dict(http.params)
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None
        if change == "attempt":
            task.retry_count += 1
        elif change == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "pending":
            task.status = "pending"
        elif change == "terminal":
            task.status = "cancelled"
        elif change == "owner":
            owner = await session.get(User, takeover.library.owner)
            assert owner is not None
            owner.status = "disabled"
        elif change == "task":
            params["task_id"] = str(uuid4())
        before = task.lease_until
    response = await http.client.post(
        http.path + "/lease", params=params, json={"lease_attempt": 3}
    )
    assert response.status_code in {404, 409}, response.text
    async with takeover.library.database() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None and task.lease_until is not None and before is not None
        assert task.lease_until.replace(tzinfo=UTC) == before.replace(tzinfo=UTC)


@pytest.mark.parametrize("attempt", [True, 0, -1, "3", 2147483648])
async def test_takeover_renewal_rejects_malformed_attempts(
    takeover_http: TakeoverHTTP, attempt: object
) -> None:
    """
    布尔值、字符串或越界数字不能充当领取序号。

    :param takeover_http (TakeoverHTTP): 认证入口
    :param attempt (object): 畸形输入
    """
    http = takeover_http
    response = await http.client.post(
        http.path + "/lease", params=http.params, json={"lease_attempt": attempt}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("route", ["capture", "file"])
async def test_takeover_body_wait_allows_concurrent_renewal_and_rechecks_revocation(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness, route: str
) -> None:
    """
    持有网络正文时另一个事务能续租，撤销随后仍阻止原请求变更。

    :param takeover_http (TakeoverHTTP): 认证入口
    :param takeover (TakeoverHarness): 数据库
    :param route (str): 网络接收阶段
    """
    import json

    http = takeover_http
    await prepare_attempt(takeover, http, lease_seconds=30)
    manifest = tree({"state": b"retained"})
    capture = takeover.capture(http.receipt, manifest)
    params = dict(http.params)
    if route == "capture":
        path, method, body = http.path + "/capture", "POST", capture.model_dump_json().encode()
    else:
        receipt = await takeover.begin(http.receipt, capture)
        params["upload_id"] = str(receipt.upload_id)
        path, method, body = http.path + "/files/" + manifest.entries[0].sha256, "PUT", b"retained"
    arrived, resume = asyncio.Event(), asyncio.Event()

    async def paused_body() -> AsyncIterator[bytes]:
        """
        在初步授权之后停住网络流，使独立事务真实竞争锁。

        :return AsyncIterator[bytes]: 分段请求正文
        """
        arrived.set()
        await resume.wait()
        yield body

    sending = asyncio.create_task(
        http.client.request(method, path, params=params, content=paused_body())
    )
    try:
        await asyncio.wait_for(arrived.wait(), 10)
        renewal = await asyncio.wait_for(
            http.client.post(http.path + "/lease", params=http.params, json={"lease_attempt": 3}),
            10,
        )
        assert renewal.status_code == 200, renewal.text
        async with takeover.library.database.begin() as session:
            owner = await session.get(User, takeover.library.owner)
            assert owner is not None
            owner.status = "disabled"
        resume.set()
        rejected = await asyncio.wait_for(sending, 10)
        assert (
            rejected.status_code == 404
            and json.loads(rejected.content)["errors"][0]["code"] == "TAKEOVER_NOT_FOUND"
        )
    finally:
        resume.set()
        if not sending.done():
            sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)


async def test_takeover_concurrent_renewals_keep_one_attempt(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    独立连接并发续租不会新增任务轮次或覆盖终态。

    :param takeover_http (TakeoverHTTP): 原始预约
    :param takeover (TakeoverHarness): 独立连接数据库
    """
    http = takeover_http
    await prepare_attempt(takeover, http)

    async def renew() -> datetime:
        """
        每次使用独立事务和同一领取序号。

        :return datetime: 数据库接受的截止时间
        """
        async with takeover.library.database.begin() as session:
            result = await takeover.service(session).renew_lease(
                takeover.node,
                http.receipt.id,
                http.receipt.task_id,
                SkillTakeoverLeaseRequest(lease_attempt=3),
            )
            return result.lease_until

    deadlines = await asyncio.gather(renew(), renew())
    async with takeover.library.database() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert task is not None and task.retry_count == 3 and task.lease_until is not None
        assert task.lease_until.replace(tzinfo=UTC) == max(deadlines)


@pytest.mark.parametrize("duration", [0, 600])
async def test_takeover_renewal_has_bounded_positive_duration(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness, duration: int
) -> None:
    """
    部署配置不能授予无限长或负向租约。

    :param takeover_http (TakeoverHTTP): 认证入口
    :param takeover (TakeoverHarness): 可替换部署设置
    :param duration (int): 配置的秒数
    """
    http = takeover_http
    await prepare_attempt(takeover, http)
    takeover.settings.node_task_lease_seconds = duration
    result = await http.client.post(
        http.path + "/lease", params=http.params, json={"lease_attempt": 3}
    )
    if duration == 0:
        assert (
            result.status_code == 409
            and result.json()["errors"][0]["code"] == "TAKEOVER_LEASE_UNAVAILABLE"
        )
    else:
        assert result.status_code == 200, result.text
        data = result.json()["data"]
        assert (
            datetime.fromisoformat(data["lease_until"])
            - datetime.fromisoformat(data["server_time"])
        ).total_seconds() == 300


async def test_postgresql_poll_cannot_reissue_a_locked_renewal(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    真实 PostgreSQL 中，轮询必须跳过正在提交续租的旧截止时间行。

    :param takeover_http (TakeoverHTTP): 认证预约
    :param takeover (TakeoverHarness): 独立数据库连接
    """
    from agent_remote_server.repositories.nodes import NodeRepository

    http = takeover_http
    await prepare_attempt(takeover, http)
    async with takeover.library.database.begin() as first:
        if first.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
        original = await first.get(NodeTask, http.receipt.task_id)
        assert original is not None and original.lease_until is not None
        poll_time = original.lease_until + timedelta(seconds=1)
        renewed = await takeover.service(first).renew_lease(
            takeover.node,
            http.receipt.id,
            http.receipt.task_id,
            SkillTakeoverLeaseRequest(lease_attempt=3),
        )
        assert renewed.lease_until > poll_time
        async with takeover.library.database.begin() as second:
            selected = await asyncio.wait_for(
                NodeRepository(second).list_pollable_tasks(
                    node_id=takeover.node, now=poll_time, limit=1
                ),
                2,
            )
            assert list(selected) == []
    async with takeover.library.database() as session:
        task = await session.get(NodeTask, http.receipt.task_id)
        assert (
            task is not None and task.retry_count == 3 and task.lease_until == renewed.lease_until
        )


async def test_postgresql_poll_claim_excludes_another_transaction(
    takeover_http: TakeoverHTTP, takeover: TakeoverHarness
) -> None:
    """
    尚未写入任务状态时，第一条领取查询已经排除第二个领取事务。

    :param takeover_http (TakeoverHTTP): 认证预约
    :param takeover (TakeoverHarness): 独立连接工厂
    """
    from agent_remote_server.repositories.nodes import NodeRepository

    async with takeover.library.database.begin() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
        task = await session.get(NodeTask, takeover_http.receipt.task_id)
        assert task is not None
        task.status, task.lease_until = "pending", None
    async with takeover.library.database.begin() as first:
        held = await NodeRepository(first).list_pollable_tasks(
            node_id=takeover.node, now=datetime.now(UTC), limit=1
        )
        assert len(held) == 1
        async with takeover.library.database.begin() as second:
            selected = await asyncio.wait_for(
                NodeRepository(second).list_pollable_tasks(
                    node_id=takeover.node, now=datetime.now(UTC), limit=1
                ),
                2,
            )
            assert list(selected) == []
    async with takeover.library.database.begin() as next_request:
        released = await NodeRepository(next_request).list_pollable_tasks(
            node_id=takeover.node, now=datetime.now(UTC), limit=1
        )
        assert len(released) == 1
