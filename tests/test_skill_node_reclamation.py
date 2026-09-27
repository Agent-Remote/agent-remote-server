"""
验证节点回收授权必须重新证明完整内容可用，而非仅重放历史保存回执。
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import update
from test_skill_content_service import database as database
from test_skill_finalization import service
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Session, User
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SkillFinalization
from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillContentObject, SkillStoredTree
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError


@pytest.mark.parametrize("outcome", ["published", "conflicted", "detached"])
async def test_reclamation_authorization_checks_original_complete_input(
    stopped: RuntimeHarness, tmp_path: Path, outcome: str
) -> None:
    """
    发布、完整冲突及异常归档都只证明原始输入的远端副本，不改账户或历史。

    :param stopped (RuntimeHarness): 原始终态会话
    :param tmp_path (Path): 私有内容卷
    :param outcome (str): 真实发布结果
    """
    if outcome == "conflicted":
        other = await new_session(stopped, tmp_path)
        first = await ingest(other, tmp_path, {"learning/new.txt": b"other"})
        await publish(other, tmp_path, first)
    receipt_id = await ingest(
        stopped, tmp_path, {"learning/new.txt": b"preserve"}, unclean=outcome == "detached"
    )
    publication = await publish(stopped, tmp_path, receipt_id)
    assert publication.status == outcome
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path)
        before = await svc.get(stopped.node, receipt_id)
        authority = await svc.authorize_reclamation(stopped.node, receipt_id, uuid4())
        after = await svc.get(stopped.node, receipt_id)
        assert before == after
        assert authority.node_id == stopped.node and authority.user_id == stopped.owner
        assert authority.account_id == stopped.account and authority.session_id == stopped.session
        assert authority.snapshot_id == stopped.snapshot and authority.finalization_id == receipt_id
        assert authority.checkpoint_id == before.checkpoint_id
        assert authority.tree_digest == before.incoming_digest
        assert authority.unclean == (outcome == "detached")
        assert authority.publication_id == publication.id
        assert authority.publication_attempt == publication.attempt
        assert authority.publication_status == outcome
        assert authority.verified_at.tzinfo == UTC
        assert 0 <= (datetime.now(UTC) - authority.verified_at).total_seconds() < 5
        assert (authority.expires_at - authority.verified_at).total_seconds() == 60


@pytest.mark.parametrize("damage", ["missing", "same_length", "linked", "manifest", "deleting"])
async def test_old_receipt_cannot_authorize_reclamation_after_content_damage(
    stopped: RuntimeHarness, tmp_path: Path, damage: str
) -> None:
    """
    模拟恢复时数据库与对象卷不匹配，原历史回执仍可读也不得释放节点唯一内容。

    :param stopped (RuntimeHarness): 原始终态绑定
    :param tmp_path (Path): 实际内容卷
    :param damage (str): 缺失、损坏或已标记删除方式
    """
    receipt_id = await ingest(stopped, tmp_path, {"learning/new.txt": b"original"})
    await publish(stopped, tmp_path, receipt_id)
    async with stopped.database.begin() as session:
        view = await service(session, tmp_path).get(stopped.node, receipt_id)
        tree = await session.get(SkillStoredTree, (stopped.owner, "state", view.incoming_digest))
        assert tree is not None
        manifest = SkillTreeManifest.model_validate(tree.manifest_json)
        entry = next(item for item in manifest.entries if item.path == "learning/new.txt")
        digest = entry.sha256
        if damage == "manifest":
            tree.manifest_json = {"version": 1, "entries": []}
        if damage == "deleting":
            await session.execute(
                update(SkillContentObject)
                .where(
                    SkillContentObject.user_id == stopped.owner, SkillContentObject.digest == digest
                )
                .values(status="deleting")
            )
    path = tmp_path / "objects" / str(stopped.owner) / digest[:2] / digest
    if damage in {"missing", "linked"}:
        path.unlink()
    if damage == "same_length":
        path.chmod(0o600)
        path.write_bytes(b"modified")
        path.chmod(0o400)
    if damage == "linked":
        unrelated = tmp_path / "unrelated"
        unrelated.write_bytes(b"original")
        path.symlink_to(unrelated)
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path)
        assert (await svc.get(stopped.node, receipt_id)).checkpoint_id == view.checkpoint_id
        with pytest.raises(SkillContentError) as error:
            await svc.authorize_reclamation(stopped.node, receipt_id, uuid4())
        assert error.value.code == "STATE_RECLAMATION_UNAVAILABLE"


@pytest.mark.parametrize("retired", ["finalization", "checkpoint", "publication"])
async def test_retired_history_cannot_borrow_equal_retained_bytes(
    stopped: RuntimeHarness, tmp_path: Path, retired: str
) -> None:
    """
    即使物理内容仍被当前 head 保留，过期的原输入也不能重新取得回收授权。

    :param stopped (RuntimeHarness): 精确原始绑定
    :param tmp_path (Path): 私有对象卷
    :param retired (str): 已退役的原身份
    """
    receipt_id = await ingest(stopped, tmp_path, {"learning/new.txt": b"retained"})
    publication = await publish(stopped, tmp_path, receipt_id)
    async with stopped.database.begin() as session:
        receipt = await session.get(SkillFinalization, receipt_id)
        assert receipt is not None
        if retired == "finalization":
            receipt.content_retired_at = datetime.now(UTC)
        elif retired == "checkpoint":
            checkpoint = await session.get(SkillCheckpoint, receipt.checkpoint_id)
            assert checkpoint is not None
            checkpoint.retained, checkpoint.tree_digest = False, None
        else:
            row = await session.get(SkillPublication, publication.id)
            assert row is not None
            row.content_retired_at = datetime.now(UTC)
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).authorize_reclamation(
                stopped.node, receipt_id, uuid4()
            )
    assert error.value.code == "STATE_RECLAMATION_UNAVAILABLE"


async def test_reclamation_requires_publication_owner_and_original_stopped_node(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    已保存但未发布、其他节点、恢复运行和用户禁用均不构成删除授权。

    :param stopped (RuntimeHarness): 原始终态会话
    :param tmp_path (Path): 内容卷
    """
    receipt_id = await ingest(stopped, tmp_path, {"learning/new.txt": b"retained"})
    with pytest.raises(SkillContentError, match="terminal"):
        async with stopped.database.begin() as session:
            await service(session, tmp_path).authorize_reclamation(
                stopped.node, receipt_id, uuid4()
            )
    await publish(stopped, tmp_path, receipt_id)
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).authorize_reclamation(uuid4(), receipt_id, uuid4())
    assert error.value.code == "FINALIZATION_NOT_FOUND"

    async with stopped.database.begin() as session:
        await session.execute(
            update(Session).where(Session.id == stopped.session).values(status="running")
        )
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).authorize_reclamation(
                stopped.node, receipt_id, uuid4()
            )
    assert error.value.code == "STATE_WRITERS_ACTIVE"
    async with stopped.database.begin() as session:
        await session.execute(
            update(User).where(User.id == stopped.owner).values(status="disabled")
        )
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).authorize_reclamation(
                stopped.node, receipt_id, uuid4()
            )
    assert error.value.code == "FINALIZATION_NOT_FOUND"


async def test_reclamation_verifies_large_runtime_object_through_its_last_byte(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    超过安装包单文件建议上限的运行数据必须全量核验，末尾损坏不能通过前缀检查。

    :param stopped (RuntimeHarness): 原始终态绑定
    :param tmp_path (Path): 私有对象卷
    """
    content = b"\x00" * (11 * 1024 * 1024) + b"last byte"
    receipt_id = await ingest(stopped, tmp_path, {"learning/state.bin": content})
    await publish(stopped, tmp_path, receipt_id)
    async with stopped.database.begin() as session:
        await service(session, tmp_path).authorize_reclamation(stopped.node, receipt_id, uuid4())
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / "objects" / str(stopped.owner) / digest[:2] / digest
    path.chmod(0o600)
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"x")
    path.chmod(0o400)
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).authorize_reclamation(
                stopped.node, receipt_id, uuid4()
            )
    assert error.value.code == "STATE_RECLAMATION_UNAVAILABLE"
