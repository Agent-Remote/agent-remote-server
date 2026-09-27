"""
验证真实节点认证、精确任务授权以及有界文件下载接口。
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve

from agent_remote_server.api.deps import get_session
from agent_remote_server.config import Settings
from agent_remote_server.main import create_app
from agent_remote_server.models import Node, NodeTask, Session, User
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.security.tokens import hash_token


@pytest.fixture
async def node_client(prepared: RuntimeHarness, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """
    使用真实节点令牌验证器，数据库与文件均来自已预约快照。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 内容卷
    :return AsyncIterator[AsyncClient]: 认证 HTTP 客户端
    """
    snapshot = await reserve(prepared, tmp_path)
    prepared.snapshot = snapshot.id
    node_token = f"node-skill-test-token:{prepared.node}"
    settings = Settings(
        secret_key="skill-node-test",
        log_level="CRITICAL",
        skill_manager_enabled=True,
        skill_storage_root=tmp_path / "objects",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    async with prepared.database.begin() as session:
        await session.execute(
            update(Node)
            .where(Node.id == prepared.node)
            .values(node_token_hash=hash_token(settings.secret_key, node_token))
        )
        await session.execute(
            update(NodeTask)
            .where(NodeTask.id == prepared.task)
            .values(status="leased", lease_until=datetime.now(UTC) + timedelta(minutes=5))
        )
    app = create_app(settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        提供共享工厂中的独立请求事务，失败由上下文回滚。

        :return AsyncIterator[AsyncSession]: 请求数据库会话
        """
        async with prepared.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {node_token}"},
    ) as client:
        yield client
    await app.state.database_engine.dispose()


async def test_exact_snapshot_manifest_and_verified_file_download(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    节点获取固定身份、原始规则代数与清单内实际内容。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 快照绑定
    """
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}"
    response = await node_client.get(path, params={"task_id": str(prepared.task)})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["node_id"] == str(prepared.node) and data["session_id"] == str(prepared.session)
    assert data["library_generation"] == 1 and data["items"][0]["entry_name"] == "learning"
    file = next(entry for entry in data["manifest"]["entries"] if entry["kind"] == "file")
    download = await node_client.get(
        path + "/files/" + file["sha256"], params={"task_id": str(prepared.task)}
    )
    assert download.status_code == 200 and b"name: learning" in download.content
    assert int(download.headers["content-length"]) == len(download.content)
    assert download.headers["etag"] == '"' + file["sha256"] + '"'


async def test_snapshot_cannot_download_other_content_of_same_user(
    node_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    即使同一用户的另一包已经上传，当前节点快照也没有读取权限。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 快照绑定
    :param tmp_path (Path): 私有内容卷
    """
    import hashlib

    content = b"other private data"
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.candidate(name="other", content=content)
    digest = hashlib.sha256(content).hexdigest()
    response = await node_client.get(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/files/{digest}",
        params={"task_id": str(prepared.task)},
    )
    assert (
        response.status_code == 404 and response.json()["errors"][0]["code"] == "CONTENT_NOT_FOUND"
    )


@pytest.mark.parametrize(
    "condition",
    [
        "task",
        "snapshot",
        "lease",
        "cancelled",
        "terminal_task",
        "stopped_session",
        "disabled_owner",
        "payload",
    ],
)
async def test_each_request_rechecks_binding_and_lifecycle(
    node_client: AsyncClient, prepared: RuntimeHarness, condition: str
) -> None:
    """
    之前成功的准备权限不能跨任务、租约或生命周期继续使用。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 快照绑定
    :param condition (str): 失效条件
    """
    task_id, snapshot_id = prepared.task, prepared.snapshot
    async with prepared.database.begin() as session:
        if condition == "task":
            task_id = uuid4()
        elif condition == "snapshot":
            snapshot_id = uuid4()
        elif condition == "lease":
            await session.execute(
                update(NodeTask)
                .where(NodeTask.id == task_id)
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif condition == "cancelled":
            await session.execute(
                update(SessionSkillSnapshot)
                .where(SessionSkillSnapshot.id == snapshot_id)
                .values(status="cancelled")
            )
        elif condition == "terminal_task":
            await session.execute(
                update(NodeTask).where(NodeTask.id == task_id).values(status="succeeded")
            )
        elif condition == "stopped_session":
            await session.execute(
                update(Session).where(Session.id == prepared.session).values(status="stopped")
            )
        elif condition == "disabled_owner":
            await session.execute(
                update(User).where(User.id == prepared.owner).values(status="disabled")
            )
        else:
            await session.execute(update(NodeTask).where(NodeTask.id == task_id).values(payload={}))
    response = await node_client.get(
        f"/api/v1/node/skill-snapshots/{snapshot_id}", params={"task_id": str(task_id)}
    )
    assert (
        response.status_code == 404 and response.json()["errors"][0]["code"] == "SNAPSHOT_NOT_FOUND"
    )


async def test_another_authenticated_node_cannot_borrow_snapshot(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    有效节点凭据不能通过已知快照和任务 ID 取得其他节点内容。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 快照绑定
    """
    other_token = f"other-node-test-token:{prepared.node}"
    async with prepared.database.begin() as session:
        session.add(
            Node(
                name="另一节点",
                status="healthy",
                region_code="global",
                node_token_hash=hash_token("skill-node-test", other_token),
            )
        )
    response = await node_client.get(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}",
        params={"task_id": str(prepared.task)},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert response.status_code == 404


async def test_node_content_rejects_missing_and_invalid_credentials(
    node_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    公开路由不能绕过现有节点认证器。

    :param node_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 快照绑定
    """
    node_client.headers.clear()
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}"
    assert (await node_client.get(path, params={"task_id": str(prepared.task)})).status_code == 401
    assert (
        await node_client.get(
            path,
            params={"task_id": str(prepared.task)},
            headers={"Authorization": "Bearer invalid-test-token"},
        )
    ).status_code == 401
