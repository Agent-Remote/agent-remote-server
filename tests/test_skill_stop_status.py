"""
验证停止显示和保存进度相互独立、原始操作归属及删除后的查询。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import request, service
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Session
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.stop_status import SkillStopStatusService
from agent_remote_server.skill_manager.manifest import manifest_digest


async def test_stop_status_never_infers_durability_from_session_status(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已停止显示没有冻结凭据时仍为未知，开始上传也不代表完整保存。

    :param stopped (RuntimeHarness): 原始受管会话
    :param tmp_path (Path): 私有内容卷
    """
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, stopped.snapshot)
        assert view.status == "awaiting_node" and not view.process_stopped
        assert view.process_status == "stopped" and not view.content_retained
        with pytest.raises(SkillContentError):
            await SkillStopStatusService(session).read(uuid4(), stopped.snapshot)
        with pytest.raises(SkillContentError):
            await SkillStopStatusService(session).read(stopped.owner, uuid4())
    payload = request(stopped)
    async with stopped.database.begin() as session:
        session.add(
            SkillSnapshotTermination(
                snapshot_id=stopped.snapshot,
                incoming_digest=manifest_digest(payload.manifest),
                unclean=False,
            )
        )
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, stopped.snapshot)
        assert view.status == "local_durable" and view.process_stopped
        assert view.unclean is False and view.checkpoint_id is None
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, stopped.snapshot)
        assert view.status == "upload_pending" and view.finalization_id == receipt.id
        assert not view.content_retained and view.checkpoint_id is None


@pytest.mark.parametrize("unclean", [False, True])
async def test_saved_status_survives_session_deletion_and_reports_retirement(
    stopped: RuntimeHarness, tmp_path: Path, unclean: bool
) -> None:
    """
    完整输入、发布和保留状态独立报告，查询不要求显示会话仍存在。

    :param stopped (RuntimeHarness): 原始受管会话
    :param tmp_path (Path): 私有内容卷
    :param unclean (bool): 是否异常退出
    """
    receipt = await ingest(stopped, tmp_path, {"learning/new.txt": b"learned"}, unclean=unclean)
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, stopped.snapshot)
        assert view.status == ("persisted_unclean" if unclean else "persisted")
        assert view.content_retained and view.publication_id is None
    publication = await publish(stopped, tmp_path, receipt)
    async with stopped.database.begin() as session:
        snapshot = await session.get(SessionSkillSnapshot, stopped.snapshot)
        original = await session.get(Session, stopped.session)
        assert snapshot is not None and original is not None
        snapshot.session_id = None
        await session.flush()
        await session.delete(original)
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, stopped.snapshot)
        assert view.status == ("detached" if unclean else "published")
        assert view.process_status == "deleted" and view.session_id == stopped.session
        assert view.publication_id == publication.id and view.content_retained
    async with stopped.database.begin() as session:
        finalization = await session.get(SkillFinalization, receipt)
        assert finalization is not None
        finalization.content_retired_at = datetime.now(UTC)
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, stopped.snapshot)
        assert not view.content_retained and view.publication_id == publication.id


async def test_status_api_preserves_device_auth_and_original_owner(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    会话设备凭据可查原始操作，节点凭据和其他用户不能借操作编号越权。

    :param user_client (AsyncClient): 原始用户认证客户端
    :param stopped (RuntimeHarness): 原始受管会话
    :param tmp_path (Path): 私有内容卷
    """
    path = f"/api/v1/sessions/skill-finalizations/{stopped.snapshot}"
    for kind in ("user", "device", "node"):
        credential = await token(stopped, stopped.owner, kind)
        response = await user_client.get(path, headers={"Authorization": f"Bearer {credential}"})
        assert response.status_code == (401 if kind == "node" else 200), response.text
        if kind != "node":
            assert response.json()["data"]["operation_id"] == str(stopped.snapshot)
    other = await user(stopped.database)
    credential = await token(stopped, other)
    assert (
        await user_client.get(path, headers={"Authorization": f"Bearer {credential}"})
    ).status_code == 404
    detail = await user_client.get(f"/api/v1/sessions/{stopped.session}")
    assert detail.json()["data"]["skill_finalization_operation_id"] == str(stopped.snapshot)
    stop = await user_client.post(f"/api/v1/sessions/{stopped.session}/stop")
    assert stop.json()["data"]["skill_finalization_operation_id"] == str(stopped.snapshot)
    refused = await user_client.delete(f"/api/v1/sessions/{stopped.session}")
    assert refused.status_code == 409 and refused.json()["errors"][0]["code"] == "STATE_PENDING"
    receipt = await ingest(stopped, tmp_path, {"learning/retained.txt": b"retained"})
    await publish(stopped, tmp_path, receipt)
    deleted = await user_client.delete(f"/api/v1/sessions/{stopped.session}")
    assert deleted.status_code == 200, deleted.text
    retained = await user_client.get(path)
    assert retained.status_code == 200 and retained.json()["data"]["process_status"] == "deleted"
    assert retained.json()["data"]["status"] == "published"


async def test_status_uses_latest_publication_instead_of_stale_finalization(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    冲突、被取代和重新计算后的尝试按代次读取，不沿用旧收尾记录状态。

    :param stopped (RuntimeHarness): 同一账户原始快照
    :param tmp_path (Path): 私有内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await ingest(stopped, tmp_path, {"learning/SKILL.md": b"first"})
    second = await ingest(other, tmp_path, {"learning/SKILL.md": b"second"})
    await publish(stopped, tmp_path, first)
    conflict = await publish(other, tmp_path, second)
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, other.snapshot)
        assert view.status == "conflicted" and view.publication_id == conflict.id
    async with stopped.database.begin() as session:
        previous = await session.get(SkillPublication, conflict.id)
        assert previous is not None
        previous.status = "superseded"
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, other.snapshot)
        assert view.status == "superseded"
    next_id = uuid4()
    async with stopped.database.begin() as session:
        previous = await session.get(SkillPublication, conflict.id)
        assert previous is not None
        session.add(
            SkillPublication(
                id=next_id,
                user_id=previous.user_id,
                account_id=previous.account_id,
                finalization_id=previous.finalization_id,
                attempt=previous.attempt + 1,
                directory_epoch=previous.directory_epoch,
                status="detached",
                reason="directory_epoch_changed",
            )
        )
    async with stopped.database() as session:
        view = await SkillStopStatusService(session).read(stopped.owner, other.snapshot)
        assert view.status == "detached" and view.publication_id == next_id
