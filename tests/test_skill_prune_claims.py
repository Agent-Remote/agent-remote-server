"""
验证真实内容生命周期续扫：延迟等待、零预留租约与同摘要重建都不能混淆原资格。
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_content_service import service
from test_skill_local import source
from test_skill_prune_candidates import candidate_plan, execute_plan
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_prune_claims import SkillPruneContentClaim
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStoredTree
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def test_zero_reservation_lease_keeps_scoped_object_claim_until_later_cleanup(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    历史和树已删但零预留租约保住对象时，原账户归属继续允许稍后精确结算。

    :param prepared (RuntimeHarness): 已管理账户
    :param tmp_path (Path): 私有卷
    """
    original = await source(prepared, tmp_path, content=b"leased source")
    async with prepared.database.begin() as session:
        svc = service(session, tmp_path)
        manifest = await svc.read_tree(prepared.owner, "state", original.content_digest)
        pending = await svc.begin(prepared.owner, str(uuid4()), manifest, "state")
        assert pending.reserved_bytes == 0
        pending_id = pending.id
    plan = await candidate_plan(prepared, tmp_path)
    tree = RetentionKey("state_tree", original.content_digest)
    assert tree in plan.content.requested_trees
    result = await execute_plan(prepared, tmp_path, plan)
    assert result.retired and result.content.state_bytes == 0 and not result.content.deletion_ids
    async with prepared.database.begin() as session:
        assert await session.get(SkillStoredTree, (prepared.owner, "state", tree.identity)) is None
        claims = list(
            await session.scalars(
                select(SkillPruneContentClaim).where(
                    SkillPruneContentClaim.user_id == prepared.owner
                )
            )
        )
        assert len(claims) == 1 and claims[0].kind == "object"
        accepted = await session.get(SkillContentUpload, pending_id)
        assert accepted is not None
        accepted.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    item = await candidate_plan(prepared, tmp_path, skill="learning")
    assert not item.claims and not item.content.requested_objects
    sweep = await candidate_plan(prepared, tmp_path)
    assert not sweep.content.requested_trees and len(sweep.content.requested_objects) == 1
    assert sweep.content.state_bytes == len(b"leased source")
    result = await execute_plan(prepared, tmp_path, sweep)
    assert not result.retired and len(result.content.deletion_ids) == 1
    worker = SkillContentDeletionWorker(prepared.database, PrivateObjectStore(tmp_path / "objects"))
    assert await worker.process(result.content.deletion_ids[0]) == "complete"
    async with prepared.database() as session:
        assert not list(
            await session.scalars(
                select(SkillPruneContentClaim).where(
                    SkillPruneContentClaim.user_id == prepared.owner
                )
            )
        )


async def test_deleted_claims_do_not_authorize_same_digest_unbound_reupload(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真正删除后的同摘要新上传没有旧生命周期归属，旧历史墓碑仍在也不能重新授权回收。

    :param prepared (RuntimeHarness): 已管理账户
    :param tmp_path (Path): 私有卷
    """
    data = b"unique reincarnation"
    original = await source(prepared, tmp_path, content=data)
    async with prepared.database() as session:
        manifest = await service(session, tmp_path).read_tree(
            prepared.owner, "state", original.content_digest
        )
    plan = await candidate_plan(prepared, tmp_path)
    result = await execute_plan(prepared, tmp_path, plan)
    assert result.content.deletion_ids
    worker = SkillContentDeletionWorker(prepared.database, PrivateObjectStore(tmp_path / "objects"))
    for identity in result.content.deletion_ids:
        assert await worker.process(identity) == "complete"
    async with prepared.database.begin() as session:
        svc = service(session, tmp_path)
        accepted = await svc.begin(prepared.owner, str(uuid4()), manifest, "account_directory")
        for entry in manifest.entries:
            if entry.kind == "file":
                await svc.put_file(prepared.owner, accepted.id, entry.sha256, io.BytesIO(data))
        recreated = await svc.complete(prepared.owner, accepted.id)
        assert recreated.digest == original.content_digest
    sweep = await candidate_plan(prepared, tmp_path)
    assert not sweep.claims
    assert RetentionKey("state_tree", original.content_digest) not in sweep.content.requested_trees
    assert sweep.content.state_bytes == 0
    await execute_plan(prepared, tmp_path, sweep)
    async with prepared.database() as session:
        assert await session.get(
            SkillStoredTree, (prepared.owner, "state", original.content_digest)
        )


async def test_claim_constraints_bind_owner_category_and_exact_content_lifetime(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    数据库拒绝无对象、分类替换和跨所有者归属；重复登记保留原身份而不刷新证据。

    :param prepared (RuntimeHarness): 已管理账户
    :param tmp_path (Path): 私有卷
    """
    import pytest
    from sqlalchemy.exc import IntegrityError

    from agent_remote_server.repositories.skill_prune_claims import SkillPruneClaimRepository

    original = await source(prepared, tmp_path, content=b"claim constraint source")
    valid: dict[str, object] = dict(
        user_id=prepared.owner,
        account_id=prepared.account,
        source_key="*",
        kind="tree",
        digest=original.content_digest,
        category="state",
        tree_digest=original.content_digest,
        object_digest=None,
    )
    invalid = (
        {"tree_digest": None},
        {"tree_digest": "f" * 64},
        {"kind": "object"},
        {"category": "package"},
        {"user_id": uuid4()},
        {"account_id": uuid4()},
    )
    async with prepared.database.begin() as session:
        for changes in invalid:
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(SkillPruneContentClaim(**(valid | changes)))
                    await session.flush()
        repository = SkillPruneClaimRepository(session)
        keys = {RetentionKey("state_tree", original.content_digest)}
        await repository.register(prepared.owner, prepared.account, "*", keys)
        key = (prepared.owner, prepared.account, "*", "tree", original.content_digest)
        claim = await session.get(SkillPruneContentClaim, key)
        assert claim is not None
        identity, created = claim.id, claim.created_at
        await repository.register(prepared.owner, prepared.account, "*", keys)
        await session.refresh(claim)
        assert (claim.id, claim.created_at) == (identity, created)
