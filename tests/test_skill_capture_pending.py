"""
验证停止观察与完整冻结分离，失败捕获不能伪装为持久化或改变原始退出分类。
"""

import asyncio
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_finalization import request
from test_skill_snapshots import prepared as prepared
from test_skill_start_results import start_result
from test_skill_termination import termination_input

from agent_remote_server.models import Session
from agent_remote_server.models.skill_snapshots import SkillFinalization
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.services.skills.stop_status import SkillStopStatusService


@pytest.mark.parametrize("unclean", [False, True])
async def test_capture_pending_confirms_stop_without_inventing_frozen_content(
    node_client: AsyncClient, prepared: RuntimeHarness, unclean: bool
) -> None:
    """
    首次额度故障确认停止，完整冻结升级后旧故障重放不能降低保存状态。

    :param node_client (AsyncClient): 原始节点 HTTP 身份
    :param prepared (RuntimeHarness): 原始受管快照
    :param unclean (bool): Helper 保留的退出分类
    """
    await start_result(prepared)
    frozen = await termination_input(prepared, unclean)
    pending = dict(frozen)
    pending.pop("incoming_digest")
    pending["capture_error"] = "quota_exceeded"
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}"
    response = await node_client.post(path + "/capture-pending", json=pending)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == pending
    assert (
        await node_client.post(path + "/capture-pending", json=pending)
    ).json() == response.json()
    async with prepared.database() as session:
        view = await SkillStopStatusService(session).read(prepared.owner, prepared.snapshot)
        assert view.status == "capture_pending" and view.process_stopped
        assert view.capture_error == "quota_exceeded" and view.unclean is unclean
        assert not view.content_retained and view.checkpoint_id is None
        assert view.finalization_id is None and view.publication_id is None
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == ("interrupted" if unclean else "stopped")
        receipt = await session.get(SkillSnapshotTermination, prepared.snapshot)
        assert receipt is not None and receipt.incoming_digest is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillFinalization)
                .where(SkillFinalization.snapshot_id == prepared.snapshot)
            )
            == 0
        )
    wrong = await node_client.post(path + "/termination", json=dict(frozen, unclean=not unclean))
    assert wrong.status_code == 409
    wrong_upload = await node_client.post(
        path + "/finalization", json=request(prepared, unclean=not unclean).model_dump(mode="json")
    )
    assert wrong_upload.status_code == 409
    assert (await node_client.post(path + "/termination", json=frozen)).status_code == 200
    assert (await node_client.post(path + "/capture-pending", json=pending)).status_code == 200
    async with prepared.database() as session:
        view = await SkillStopStatusService(session).read(prepared.owner, prepared.snapshot)
        assert view.status == "local_durable" and view.capture_error is None
        assert view.process_stopped and not view.content_retained
        receipt = await session.get(SkillSnapshotTermination, prepared.snapshot)
        assert receipt is not None and receipt.incoming_digest == frozen["incoming_digest"]
    assert (
        await node_client.post(
            path + "/finalization", json=request(prepared, unclean=unclean).model_dump(mode="json")
        )
    ).status_code == 200


@pytest.mark.parametrize("fault", ["snapshot", "task", "generation", "code", "raw_error"])
async def test_capture_pending_cannot_replace_original_authority(
    node_client: AsyncClient, prepared: RuntimeHarness, fault: str
) -> None:
    """
    错误身份与任意诊断内容不能产生停止凭据或改变会话。

    :param node_client (AsyncClient): 原始节点 HTTP 身份
    :param prepared (RuntimeHarness): 固定快照
    :param fault (str): 替换的受限输入
    """
    await start_result(prepared)
    pending = await termination_input(prepared)
    pending.pop("incoming_digest")
    pending["capture_error"] = "quota_exceeded"
    snapshot = prepared.snapshot
    if fault == "snapshot":
        snapshot = uuid4()
    elif fault == "task":
        pending["task_id"] = str(uuid4())
    elif fault == "generation":
        pending["library_generation"] = 999
    elif fault == "code":
        pending["capture_error"] = "private-untrusted-diagnostic"
    else:
        pending["message"] = "private-untrusted-diagnostic"
    response = await node_client.post(
        f"/api/v1/node/skill-snapshots/{snapshot}/capture-pending", json=pending
    )
    assert response.status_code in {404, 409, 422}
    async with prepared.database() as session:
        assert await session.get(SkillSnapshotTermination, prepared.snapshot) is None
        runtime = await session.get(Session, prepared.session)
        assert runtime is not None and runtime.status == "starting"


@pytest.mark.parametrize("conflict", [False, True])
async def test_postgresql_pending_and_frozen_observations_serialize(
    node_client: AsyncClient, prepared: RuntimeHarness, conflict: bool
) -> None:
    """
    独立事务并发上报只允许同一退出分类，成功冻结不会被迟到故障降级。

    :param node_client (AsyncClient): 已认证原始节点客户端
    :param prepared (RuntimeHarness): 固定快照身份
    :param conflict (bool): 冻结请求是否篡改退出分类
    """
    async with prepared.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires independent PostgreSQL row locks")
    await start_result(prepared)
    frozen = await termination_input(prepared, conflict)
    pending = {key: value for key, value in frozen.items() if key != "incoming_digest"}
    pending.update(unclean=False, capture_error="quota_exceeded")
    path = f"/api/v1/node/skill-snapshots/{prepared.snapshot}"
    responses = await asyncio.gather(
        node_client.post(path + "/capture-pending", json=pending),
        node_client.post(path + "/termination", json=frozen),
    )
    assert sorted(response.status_code for response in responses) == (
        [200, 409] if conflict else [200, 200]
    )
    async with prepared.database() as session:
        retained = await session.get(SkillSnapshotTermination, prepared.snapshot)
        assert retained is not None
        if not conflict:
            assert retained.incoming_digest == frozen["incoming_digest"]
            assert retained.capture_error is None and not retained.unclean
        elif responses[0].status_code == 200:
            assert retained.incoming_digest is None and not retained.unclean
        else:
            assert retained.incoming_digest == frozen["incoming_digest"] and retained.unclean
