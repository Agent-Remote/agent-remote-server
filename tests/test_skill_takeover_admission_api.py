"""
验证既有用户和设备启动能取得接管身份，而状态查询保持隔离和只读。
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from skill_takeover_support import takeover as takeover
from skill_takeover_support import tree
from sqlalchemy.ext.asyncio import AsyncSession
from takeover_admission_support import TakeoverAdmission, admission_token
from takeover_admission_support import admission as admission
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_library import library as library

from agent_remote_server.api.deps import get_session
from agent_remote_server.main import create_app
from agent_remote_server.models import NodeTask, Session
from agent_remote_server.models.skill_takeover import SkillAccountTakeover


@pytest.fixture
async def client(admission: TakeoverAdmission) -> AsyncIterator[AsyncClient]:
    """
    创建真实用户认证的独立请求事务客户端。

    :param admission (TakeoverAdmission): 原始账户与启动输入
    :return AsyncIterator[AsyncClient]: 真实路由客户端
    """
    state = admission.state
    token = await admission_token(admission, state.library.owner, "user")
    app = create_app(state.settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        失败请求离开后回滚未提交内容。

        :return AsyncIterator[AsyncSession]: 当前请求事务
        """
        async with state.library.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        yield http
    await app.state.database_engine.dispose()


@pytest.mark.parametrize("kind", ["user", "device"])
async def test_http_takeover_admission_and_owner_progress(
    admission: TakeoverAdmission, client: AsyncClient, kind: str
) -> None:
    """
    用户和设备复用相同预约，其他用户和节点凭据不能查询其元数据。

    :param admission (TakeoverAdmission): 原始启动输入
    :param client (AsyncClient): 真实路由客户端
    :param kind (str): 当前会话认证类型
    """
    state = admission.state
    token = await admission_token(admission, state.library.owner, kind)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post("/api/v1/sessions", json=admission.payload(), headers=headers)
    assert response.status_code == 409
    details = response.json()["error"]["details"]
    assert details["reservation_committed"] and details["session_created"] is False
    path = f"/api/v1/sessions/skill-takeovers/{details['takeover_id']}"
    progress = await client.get(path, headers=headers)
    assert progress.status_code == 200
    data = progress.json()["data"]
    assert data == {
        "operation_id": details["takeover_id"],
        "account_id": str(state.account),
        "status": "reserved",
        "task_status": "pending",
        "checkpoint_id": None,
        "recovery_required": False,
    }
    repeated = await client.post("/api/v1/sessions", json=admission.payload(), headers=headers)
    assert repeated.json()["error"]["details"] == details
    outsider = await user(state.library.database)
    for owner, role, expected in [(outsider, "user", 404), (state.library.owner, "node", 401)]:
        other = await admission_token(admission, owner, role)
        assert (
            await client.get(path, headers={"Authorization": f"Bearer {other}"})
        ).status_code == expected
    assert (
        await client.get(f"/api/v1/sessions/skill-takeovers/{uuid4()}", headers=headers)
    ).status_code == 404
    state.settings.skill_manager_enabled = False
    assert (await client.get(path, headers=headers)).status_code == 200
    rejected = await client.post("/api/v1/sessions", json=admission.payload(), headers=headers)
    assert rejected.json()["error"]["code"] == "SKILL_MANAGER_DISABLED"


async def test_http_progress_tracks_commit_without_renewing_or_completing_task(
    admission: TakeoverAdmission, client: AsyncClient
) -> None:
    """
    只读进度分别呈现预约、上传和权威提交，不能代替任务确认或续租。

    :param admission (TakeoverAdmission): 原始启动输入
    :param client (AsyncClient): 真实路由客户端
    """
    state = admission.state
    response = await client.post("/api/v1/sessions", json=admission.payload())
    identity = UUID(response.json()["error"]["details"]["takeover_id"])
    path = f"/api/v1/sessions/skill-takeovers/{identity}"
    async with state.library.database.begin() as session:
        receipt = await session.get(SkillAccountTakeover, identity)
        original = await session.get(Session, admission.original)
        assert receipt is not None and original is not None
        original.status = "stopped"
    await state.lease(receipt)
    uploading = await state.begin(receipt, state.capture(receipt, tree({})))
    assert (await client.get(path)).json()["data"]["status"] == "uploading"
    completed = await state.complete(uploading)
    async with state.library.database() as session:
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None
        before = task.status, task.lease_until, task.retry_count
    data = (await client.get(path)).json()["data"]
    assert data["status"] == "committed" and data["checkpoint_id"] == str(completed.checkpoint_id)
    async with state.library.database() as session:
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None and (task.status, task.lease_until, task.retry_count) == before


async def test_http_progress_reports_cancelled_pending_reservation(
    admission: TakeoverAdmission, client: AsyncClient
) -> None:
    """
    终态任务不能伪装成接管完成，读取不会重新派发或解除围栏。

    :param admission (TakeoverAdmission): 原始启动输入
    :param client (AsyncClient): 真实路由客户端
    """
    state = admission.state
    response = await client.post("/api/v1/sessions", json=admission.payload())
    identity = UUID(response.json()["error"]["details"]["takeover_id"])
    async with state.library.database.begin() as session:
        receipt = await session.get(SkillAccountTakeover, identity)
        assert receipt is not None
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None
        task.status = "cancelled"
    data = (await client.get(f"/api/v1/sessions/skill-takeovers/{identity}")).json()["data"]
    assert data["status"] == "reserved" and data["recovery_required"] is True
    assert data["task_status"] == "cancelled" and data["checkpoint_id"] is None
