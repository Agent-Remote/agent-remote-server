"""
验证精确终止观察独立于完整会话清单和已过期启动租约。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_finalization import request
from test_skill_snapshots import prepared as prepared
from test_skill_start_results import start_result

from agent_remote_server.config import Settings
from agent_remote_server.models import (
    DeviceSession,
    Node,
    NodeTask,
    NodeTaskResult,
    Session,
    User,
    UserDevice,
)
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.security.tokens import hash_token
from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.sessions import ToolSessionService
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.manifest import manifest_digest


async def termination_input(prepared: RuntimeHarness, unclean: bool = False) -> dict[str, object]:
    """
    用固定快照和同一完整收尾清单构造精确观察。

    :param prepared (RuntimeHarness): 原始预约身份
    :param unclean (bool): 原始异常退出分类
    :return dict[str, object]: 不含内容或宿主路径的请求
    """
    async with prepared.database() as session:
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert snapshot is not None
        return {
            "session_id": str(prepared.session),
            "task_id": str(prepared.task),
            "initial_tree_digest": snapshot.tree_digest,
            "directory_epoch": snapshot.directory_epoch,
            "library_generation": snapshot.library_generation,
            "incoming_digest": manifest_digest(request(prepared).manifest),
            "unclean": unclean,
        }


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("unclean", [False, True])
async def test_termination_fences_late_start_and_allows_finalization(
    node_client: AsyncClient, prepared: RuntimeHarness, accepted: bool, unclean: bool
) -> None:
    """
    原始启动是否已确认都不能在退出后复活，收尾无需完整清单对账。

    :param node_client (AsyncClient): 原节点认证客户端
    :param prepared (RuntimeHarness): 原始快照身份
    :param accepted (bool): 是否已接收启动收据
    :param unclean (bool): 原始退出分类
    """
    ready = await start_result(prepared)
    start_path = f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
    if accepted:
        assert (await node_client.post(start_path, json=ready)).status_code == 200
    async with prepared.database.begin() as session:
        task = await session.get(NodeTask, prepared.task)
        assert task is not None
        task.lease_until = None
    payload = await termination_input(prepared, unclean)
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination"
    response = await node_client.post(path, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["committed"] and response.json()["status"] == "stopped"
    assert response.json()["data"] == payload
    assert (await node_client.post(path, json=payload)).json() == response.json()
    observation = await node_client.post(start_path + "/inspect", json=ready)
    assert observation.status_code == 200, observation.text
    assert observation.json()["committed"] is accepted
    assert observation.json()["status"] == ("completed" if accepted else "unconfirmed")
    assert observation.json()["data"] == {
        "result": ready,
        "accepted": accepted,
        "current_lease_attempt": 3,
        "task_status": "succeeded" if accepted else "cancelled",
    }
    late = await node_client.post(start_path, json=ready)
    assert late.status_code == (200 if accepted else 409)
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        task = await session.get(NodeTask, prepared.task)
        assert runtime is not None and runtime.status == ("interrupted" if unclean else "stopped")
        assert task is not None and task.status == ("succeeded" if accepted else "cancelled")
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillSnapshotTermination)
                .where(SkillSnapshotTermination.snapshot_id == prepared.snapshot)
            )
            == 1
        )
        assert await session.scalar(
            select(func.count())
            .select_from(NodeTaskResult)
            .where(NodeTaskResult.node_task_id == prepared.task)
        ) == int(accepted)
    rejected = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=request(prepared, b"changed", unclean=unclean).model_dump(mode="json"),
    )
    assert rejected.status_code == 409
    begin = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=request(prepared, unclean=unclean).model_dump(mode="json"),
    )
    assert begin.status_code == 200, begin.text
    changed = dict(payload, unclean=not unclean)
    assert (await node_client.post(path, json=changed)).status_code == 409
    mismatch = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=request(prepared, b"changed", unclean=unclean).model_dump(mode="json"),
    )
    assert mismatch.status_code == 409


@pytest.mark.parametrize(
    "field",
    ["session_id", "task_id", "initial_tree_digest", "directory_epoch", "library_generation"],
)
async def test_termination_rejects_changed_original_binding(
    node_client: AsyncClient, prepared: RuntimeHarness, field: str
) -> None:
    """
    其他身份或配置代数不能终止原会话或写入收据。

    :param node_client (AsyncClient): 原节点认证客户端
    :param prepared (RuntimeHarness): 精确预约
    :param field (str): 故障注入字段
    """
    await start_result(prepared)
    payload = await termination_input(prepared)
    payload[field] = (
        str(uuid4()) if field.endswith("_id") else "a" * 64 if field.endswith("digest") else 999
    )
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination", json=payload
    )
    assert response.status_code in {404, 409}
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "starting"
        assert await session.get(SkillSnapshotTermination, prepared.snapshot) is None


@pytest.mark.parametrize(
    "fault",
    [
        "other_node",
        "inactive_owner",
        "missing_marker",
        "boolean_marker",
        "extra",
        "boolean_generation",
    ],
)
async def test_termination_denies_foreign_or_malformed_authority(
    node_client: AsyncClient, prepared: RuntimeHarness, fault: str
) -> None:
    """
    节点令牌、用户状态和原始受管标记不能由请求字段绕过。

    :param node_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 固定预约
    :param fault (str): 授权故障类型
    """
    await start_result(prepared)
    payload = await termination_input(prepared)
    headers = {}
    async with prepared.database.begin() as session:
        if fault == "other_node":
            token = "termination-other-node:" + str(uuid4())
            session.add(
                Node(
                    name="其他终止节点",
                    status="healthy",
                    region_code="global",
                    node_token_hash=hash_token("skill-node-test", token),
                )
            )
            headers = {"Authorization": f"Bearer {token}"}
        elif fault == "inactive_owner":
            owner = await session.get(User, prepared.owner)
            assert owner is not None
            owner.status = "disabled"
        elif fault in {"missing_marker", "boolean_marker"}:
            task = await session.get(NodeTask, prepared.task)
            assert task is not None
            body = dict(task.payload)
            if fault == "missing_marker":
                del body["skill_manager"]
            else:
                body["skill_manager"] = {"protocol_version": True}
            task.payload = body
        elif fault == "extra":
            payload["user_id"] = str(prepared.owner)
        else:
            payload["directory_epoch"] = True
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination",
        json=payload,
        headers=headers,
    )
    assert response.status_code in {404, 409, 422}
    async with prepared.database() as session:
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "starting"
        assert await session.get(SkillSnapshotTermination, prepared.snapshot) is None


@pytest.mark.parametrize("fail_revocation", [False, True])
async def test_termination_revokes_device_atomically(
    node_client: AsyncClient,
    prepared: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
    fail_revocation: bool,
) -> None:
    """
    撤销与终止共同提交，后续浏览器撤销失败必须回滚设备和快照状态。

    :param node_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原始预约
    :param monkeypatch (pytest.MonkeyPatch): 后续撤销故障注入
    :param fail_revocation (bool): 是否模拟后续事务失败
    """
    await start_result(prepared)
    binding_id, other_id = uuid4(), uuid4()
    async with prepared.database.begin() as session:
        device_id = await session.scalar(
            select(UserDevice.id).where(UserDevice.user_id == prepared.owner)
        )
        runtime = await session.get(Session, prepared.session)
        assert device_id is not None and runtime is not None
        session.add(
            DeviceSession(
                id=binding_id,
                user_id=prepared.owner,
                device_id=device_id,
                node_id=prepared.node,
                tool_session_id=prepared.session,
                tool_session_reference_id=prepared.session,
                status="active",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        session.add(
            Session(
                id=other_id,
                user_id=prepared.owner,
                tool_account_id=prepared.account,
                node_id=prepared.node,
                workspace_id=runtime.workspace_id,
                tool_type="claude",
                project_key="other",
                runtime_backend="native",
                status="running",
            )
        )
    payload = await termination_input(prepared)
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination"
    if fail_revocation:

        async def reject(
            self: EgoBrowserService,
            *,
            tool_session_id: UUID,
            reason: str,
            commit: bool,
            publish: bool,
        ) -> int:
            """
            在设备变更之后拒绝浏览器撤销，验证整个保存点回滚。

            :param tool_session_id (UUID): 原始工具会话
            :param reason (str): 固定停止原因
            :param commit (bool): 是否提交
            :param publish (bool): 是否立即发布
            :return int: 不会返回的撤销数量
            """
            assert tool_session_id == prepared.session and not commit and not publish
            raise RuntimeError("injected browser revocation failure")

        monkeypatch.setattr(EgoBrowserService, "revoke_for_tool_session", reject)
        with pytest.raises(RuntimeError, match="injected browser"):
            await node_client.post(path, json=payload)
    else:
        response = await node_client.post(path, json=payload)
        assert response.status_code == 200, response.text
    async with prepared.database() as session:
        device = await session.get(DeviceSession, binding_id)
        runtime = await session.get(Session, prepared.session)
        other = await session.get(Session, other_id)
        assert device is not None and device.status == ("active" if fail_revocation else "stopped")
        assert runtime is not None and runtime.status == (
            "starting" if fail_revocation else "stopped"
        )
        assert other is not None and other.status == "running"
        assert (
            await session.get(SkillSnapshotTermination, prepared.snapshot) is None
        ) == fail_revocation


@pytest.mark.parametrize("race", ["startup", "same", "changed"])
async def test_postgresql_termination_serializes_original_authority(
    node_client: AsyncClient, prepared: RuntimeHarness, race: str
) -> None:
    """
    真实行锁保证迟到启动和并发终止观察只提交一份不可变停止依据。

    :param node_client (AsyncClient): 独立请求客户端
    :param prepared (RuntimeHarness): 固定预约
    :param race (str): 并发竞争类型
    """
    async with prepared.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    ready = await start_result(prepared)
    payload = await termination_input(prepared)
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination"
    other_path = (
        f"/api/v1/node-api/tasks/{prepared.task}/managed-start-result"
        if race == "startup"
        else path
    )
    other_payload = (
        ready
        if race == "startup"
        else dict(payload, unclean=True)
        if race == "changed"
        else payload
    )
    responses = await asyncio.gather(
        node_client.post(path, json=payload), node_client.post(other_path, json=other_payload)
    )
    codes = [response.status_code for response in responses]
    if race == "startup":
        assert codes[0] == 200 and codes[1] in {200, 409}
    elif race == "same":
        assert codes == [200, 200]
    else:
        assert sorted(codes) == [200, 409]
    async with prepared.database() as session:
        saved = await session.get(SkillSnapshotTermination, prepared.snapshot)
        runtime = await session.get(Session, prepared.session)
        task = await session.get(NodeTask, prepared.task)
        assert saved is not None and saved.incoming_digest == payload["incoming_digest"]
        assert runtime is not None and runtime.status == (
            "interrupted" if saved.unclean else "stopped"
        )
        assert task is not None and task.status in {"succeeded", "cancelled"}


@pytest.mark.parametrize("unclean", [False, True])
async def test_termination_cannot_authorize_delete_before_publication(
    node_client: AsyncClient, prepared: RuntimeHarness, unclean: bool
) -> None:
    """
    停止收据不允许删除唯一未保存数据，完整发布后删除展示仍保留可重放停止身份。

    :param node_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原始精确快照
    :param unclean (bool): 原始异常退出标记
    """
    await start_result(prepared)
    payload = await termination_input(prepared, unclean)
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}/termination"
    original = await node_client.post(path, json=payload)
    assert original.status_code == 200, original.text
    async with prepared.database() as session:
        owner = await session.get(User, prepared.owner)
        assert owner is not None
        with pytest.raises(SkillContentError) as failure:
            await ToolSessionService(session, Settings()).delete_session(
                user=owner, session_id=prepared.session
            )
        assert failure.value.code == "STATE_PENDING"
    content = b"---\nname: learning\n---\nlearned"
    capture = request(prepared, content, unclean=unclean)
    begin = await node_client.post(
        f"/api/v1/node/skill-snapshots/{prepared.snapshot}/finalization",
        json=capture.model_dump(mode="json"),
    )
    assert begin.status_code == 200, begin.text
    receipt = begin.json()["data"]
    base = f"/api/v1/node/skill-finalizations/{receipt['id']}"
    params = {"upload_id": receipt["upload_id"]}
    assert (
        await node_client.put(
            base + "/files/" + capture.manifest.entries[1].sha256, params=params, content=content
        )
    ).status_code == 200
    assert (await node_client.post(base + "/complete", params=params)).status_code == 200
    assert (await node_client.post(base + "/publish")).status_code == 200
    async with prepared.database() as session:
        owner = await session.get(User, prepared.owner)
        assert owner is not None
        await ToolSessionService(session, Settings()).delete_session(
            user=owner, session_id=prepared.session
        )
    replay = await node_client.post(path, json=payload)
    assert replay.status_code == 200 and replay.json() == original.json()
    async with prepared.database() as session:
        assert await session.get(Session, prepared.session) is None
        snapshot = await session.get(SessionSkillSnapshot, prepared.snapshot)
        assert (
            snapshot is not None and snapshot.session_id is None and snapshot.status == "retained"
        )
        assert await session.get(SkillSnapshotTermination, prepared.snapshot) is not None
