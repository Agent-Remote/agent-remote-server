"""
验证准备租约绑定原快照和领取轮次，并且不阻塞在文件复制上。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask, Session, User
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.repositories.nodes import NodeRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.schemas.skill_snapshot_lease import SkillSnapshotLeaseRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.node_content import NodeSkillContentService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def prepare_attempt(prepared: RuntimeHarness) -> None:
    """
    建立真实受管任务指针与即将过期的同一领取轮次。

    :param prepared (RuntimeHarness): 原始快照绑定
    """
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.retry_count = 3
        task.lease_until = datetime.now(UTC) + timedelta(seconds=15)
        task.payload = dict(task.payload) | {
            "skill_manager": {
                "protocol_version": 1,
                "manifest_version": 1,
                "snapshot_id": str(prepared.snapshot),
                "task_id": str(prepared.task),
            }
        }


async def test_snapshot_lease_preserves_exact_attempt(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    独立续期返回完整快照归属，且不改变任务轮次和原始快照。

    :param node_client (AsyncClient): 认证客户端
    :param prepared (RuntimeHarness): 原始绑定
    """
    await prepare_attempt(prepared)
    for _ in range(2):
        response = await node_client.post(
            f"/api/v1/node/skill-snapshots/{prepared.snapshot}/lease",
            params={"task_id": str(prepared.task)},
            json={"lease_attempt": 3},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "leased" and body["committed"] is False
        data = body["data"]
        for field, value in {
            "snapshot_id": prepared.snapshot,
            "task_id": prepared.task,
            "node_id": prepared.node,
            "user_id": prepared.owner,
            "account_id": prepared.account,
            "session_id": prepared.session,
        }.items():
            assert data[field] == str(value)
        assert data["runtime_backend"] == "native" and data["lease_attempt"] == 3
        duration = (
            datetime.fromisoformat(data["lease_until"])
            - datetime.fromisoformat(data["server_time"])
        ).total_seconds()
        assert 0 < duration <= 300 and 0 < data["renew_after_milliseconds"] < duration * 1000
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert task is not None and task.retry_count == 3 and task.status == "leased"
        assert snapshot is not None and snapshot.status == "reserved"


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "expired",
        "terminal",
        "pending",
        "owner",
        "session",
        "snapshot",
        "pointer",
        "task",
        "missing_pointer",
    ],
)
async def test_snapshot_lease_rejects_changed_authority(
    node_client: AsyncClient, prepared: RuntimeHarness, change: str
) -> None:
    """
    旧轮次和已撤销状态不能复活或借用其他任务的权限。

    :param node_client (AsyncClient): 认证客户端
    :param prepared (RuntimeHarness): 原始绑定
    :param change (str): 失效条件
    """
    await prepare_attempt(prepared)
    task_id = prepared.task
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        if change == "attempt":
            task.retry_count += 1
        elif change == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "terminal":
            task.status = "failed"
        elif change == "pending":
            task.status = "pending"
        elif change == "task":
            task_id = uuid4()
        elif change in {"pointer", "missing_pointer"}:
            payload = dict(task.payload)
            payload.pop("skill_manager")
            if change == "pointer":
                payload["skill_manager"] = {"snapshot_id": str(uuid4())}
            task.payload = payload
        elif change == "owner":
            owner = await session.get(User, prepared.owner)
            assert owner is not None
            owner.status = "disabled"
        elif change == "session":
            tool_session = await session.get(Session, prepared.session)
            assert tool_session is not None
            tool_session.status = "stopped"
        else:
            snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
            assert snapshot is not None
            snapshot.status = "cancelled"
        deadline = task.lease_until
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/lease",
        params={"task_id": str(task_id)},
        json={"lease_attempt": 3},
    )
    assert response.status_code in {404, 409}, response.text
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None and task.lease_until is not None and deadline is not None
        assert task.lease_until.replace(tzinfo=UTC) == deadline.replace(tzinfo=UTC)


@pytest.mark.parametrize("attempt", [True, 0, -1, 2147483648, "3", 3.0, None])
async def test_snapshot_lease_requires_strict_attempt(
    node_client: AsyncClient, prepared: RuntimeHarness, attempt: object
) -> None:
    """
    布尔值、浮点数和字符串不能伪装成领取序号。

    :param node_client (AsyncClient): 认证客户端
    :param prepared (RuntimeHarness): 原始绑定
    :param attempt (object): 非法序号
    """
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/lease",
        params={"task_id": str(prepared.task)},
        json={"lease_attempt": attempt},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("revoke", [False, True])
async def test_snapshot_copy_allows_renewal_and_rechecks_authority(
    node_client: AsyncClient,
    prepared: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
) -> None:
    """
    暂存复制期间续租可提交，撤销后即使文件复制完成也不返回内容。

    :param node_client (AsyncClient): 独立事务客户端
    :param prepared (RuntimeHarness): 原始绑定
    :param monkeypatch (pytest.MonkeyPatch): 控制磁盘复制等待
    :param revoke (bool): 是否撤销任务
    """
    await prepare_attempt(prepared)
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}"
    params = {"task_id": str(prepared.task)}
    response = await node_client.get(path, params=params)
    entry = next(
        item for item in response.json()["data"]["manifest"]["entries"] if item["kind"] == "file"
    )
    entered, resume = asyncio.Event(), asyncio.Event()
    original = PrivateObjectStore.copy_file

    async def delayed(
        self: PrivateObjectStore, owner_id: UUID, entry: SkillTreeEntry, target: BinaryIO
    ) -> None:
        """
        等待续租或撤销提交后继续真实文件验证。

        :param owner_id (UUID): 原始用户
        :param entry (SkillTreeEntry): 固定清单成员
        :param target (BinaryIO): 私有输出流
        """
        entered.set()
        await resume.wait()
        await original(self, owner_id, entry, target)

    monkeypatch.setattr(PrivateObjectStore, "copy_file", delayed)
    downloading = asyncio.create_task(
        node_client.get(path + "/files/" + entry["sha256"], params=params)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        renewed = await asyncio.wait_for(
            node_client.post(path + "/lease", params=params, json={"lease_attempt": 3}), 2
        )
        assert renewed.status_code == 200, renewed.text
        if revoke:
            async with prepared.database.begin() as session:
                task = await session.get(NodeTask, prepared.task)
                assert task is not None
                task.status = "cancelled"
        resume.set()
        result = await asyncio.wait_for(downloading, 2)
        assert result.status_code == (404 if revoke else 200), result.text
    finally:
        resume.set()
        if not downloading.done():
            downloading.cancel()
        await asyncio.gather(downloading, return_exceptions=True)


async def test_postgresql_snapshot_renewal_excludes_poll(
    node_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真实行锁阻止轮询在续租事务提交前按旧截止时间重复领取任务。

    :param node_client (AsyncClient): 已建立认证预约的客户端
    :param prepared (RuntimeHarness): 独立事务工厂
    :param tmp_path (Path): 原始内容卷
    """
    await prepare_attempt(prepared)
    async with prepared.database.begin() as first:
        if first.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL row locks")
        task = await first.get(NodeTask, prepared.task)
        assert task is not None and task.lease_until is not None
        poll_time = task.lease_until + timedelta(seconds=1)
        service = NodeSkillContentService(
            first, PrivateObjectStore(tmp_path / "objects"), Settings().skill_storage_policy
        )
        renewed = await service.renew_lease(
            prepared.node,
            prepared.snapshot,
            prepared.task,
            SkillSnapshotLeaseRequest(lease_attempt=3),
            60,
        )
        assert renewed.lease_until > poll_time
        async with prepared.database.begin() as second:
            tasks = await asyncio.wait_for(
                NodeRepository(second).list_pollable_tasks(
                    node_id=prepared.node, now=poll_time, limit=100
                ),
                2,
            )
            assert prepared.task not in {row.id for row in tasks}
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        assert (
            task is not None and task.retry_count == 3 and task.lease_until == renewed.lease_until
        )


async def test_snapshot_concurrent_renewals_keep_attempt(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    两条独立请求串行更新截止时间，既不增加轮次也不回退租约。

    :param node_client (AsyncClient): 独立请求事务客户端
    :param prepared (RuntimeHarness): 原始快照绑定
    """
    await prepare_attempt(prepared)
    replies = await asyncio.gather(
        *(
            node_client.post(
                f"/api/v1/node/skill-snapshots/{prepared.snapshot}/lease",
                params={"task_id": str(prepared.task)},
                json={"lease_attempt": 3},
            )
            for _ in range(2)
        )
    )
    assert all(reply.status_code == 200 for reply in replies)
    deadlines = [datetime.fromisoformat(reply.json()["data"]["lease_until"]) for reply in replies]
    async with prepared.database() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None and task.lease_until is not None and task.retry_count == 3
        assert task.lease_until.replace(tzinfo=UTC) == max(deadlines)


@pytest.mark.parametrize("duration", [0, -1, 600])
async def test_snapshot_lease_bounds_configured_duration(
    node_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path, duration: int
) -> None:
    """
    部署配置不能授予无限或负向租约。

    :param node_client (AsyncClient): 已建立原始任务的客户端
    :param prepared (RuntimeHarness): 原始快照绑定
    :param tmp_path (Path): 私有内容卷
    :param duration (int): 配置的秒数
    """
    await prepare_attempt(prepared)
    async with prepared.database.begin() as session:
        service = NodeSkillContentService(
            session, PrivateObjectStore(tmp_path / "objects"), Settings().skill_storage_policy
        )
        request = SkillSnapshotLeaseRequest(lease_attempt=3)
        if duration <= 0:
            with pytest.raises(SkillContentError, match="duration"):
                await service.renew_lease(
                    prepared.node, prepared.snapshot, prepared.task, request, duration
                )
        else:
            lease = await service.renew_lease(
                prepared.node, prepared.snapshot, prepared.task, request, duration
            )
            assert (lease.lease_until - lease.server_time).total_seconds() == 300
