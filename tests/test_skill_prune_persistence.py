"""
验证 prune 明细与删除任务的数据库所有者约束，以及重试进度不改写原受理。
"""

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_prune_commands import full_preview, service
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_prune_operations import (
    SkillPruneOperationDeletion,
    SkillPruneOperationEntry,
)
from agent_remote_server.schemas.skill_prune import PruneCommand
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.services.skills.prune_commands.queries import PruneQueries
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def test_receipt_owner_foreign_keys_and_retry_progress(
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    实际外键拒绝其他用户的明细与任务，物理失败只影响独立进度而非原始受理。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 注入一次真实删除接口失败
    """
    await shared_directory(stopped, tmp_path)
    secret = secrets.token_hex(32)
    last, _ = await full_preview(stopped, tmp_path, secret, limit=100)
    assert last.confirmation is not None
    request = PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation)
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path, secret).execute(stopped.owner, request)
        task_id = await session.scalar(
            select(SkillPruneOperationDeletion.deletion_id).where(
                SkillPruneOperationDeletion.operation_id == receipt.operation_id,
            )
        )
        assert task_id is not None
    other = await user(stopped.database)
    foreign_task = uuid4()
    async with stopped.database.begin() as session:
        session.add(
            SkillContentDeletion(
                id=foreign_task,
                user_id=other,
                digest="f" * 64,
                size=0,
                category_mask=2,
                status="pending",
                attempts=0,
                next_attempt_at=datetime.now(UTC),
            )
        )
    rows = (
        SkillPruneOperationEntry(
            operation_id=receipt.operation_id,
            user_id=other,
            ordinal=receipt.disclosure_rows,
            disclosure_json={},
        ),
        SkillPruneOperationEntry(
            operation_id=receipt.operation_id, user_id=stopped.owner, ordinal=-1, disclosure_json={}
        ),
        SkillPruneOperationDeletion(
            operation_id=receipt.operation_id, user_id=stopped.owner, deletion_id=foreign_task
        ),
    )
    for row in rows:
        with pytest.raises(IntegrityError):
            async with stopped.database.begin() as session:
                session.add(row)
                await session.flush()

    async def fail_delete(
        self: PrivateObjectStore, owner_id: UUID, digest: str, size: int, deletion_id: UUID
    ) -> None:
        """
        磁盘不可用只产生可重试任务，不能改变逻辑结算或最初确认。

        :param owner_id (UUID): 原所有者
        :param digest (str): 原文件摘要
        :param size (int): 原文件字节
        :param deletion_id (UUID): 原任务身份
        """
        raise OSError("test deletion failure")

    worker = SkillContentDeletionWorker(stopped.database, PrivateObjectStore(tmp_path / "objects"))
    with monkeypatch.context() as patch:
        patch.setattr(PrivateObjectStore, "delete_committed", fail_delete)
        assert await worker.process(task_id) == "pending"
    async with stopped.database.begin() as session:
        queries = PruneQueries(session)
        progress = await queries.progress(stopped.owner, receipt.operation_id)
        assert progress.retrying_tasks == 1 and progress.deleted_file_bytes == 0
        assert progress.pending_file_bytes == receipt.summary.pending_file_bytes
        assert await queries.by_id(stopped.owner, receipt.operation_id) == receipt
        task = await session.get(SkillContentDeletion, task_id)
        assert task is not None
        task.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await worker.process(task_id) == "complete"
    async with stopped.database() as session:
        queries = PruneQueries(session)
        assert (await queries.progress(stopped.owner, receipt.operation_id)).retrying_tasks == 0
        assert await queries.by_key(stopped.owner, request.idempotency_key) == receipt
