"""
验证分页只读、真实清理与不可变回执的共同事务，以及不依赖旧输入的重放。
"""

import secrets
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_compaction_reclamation import persisted_content
from test_skill_content_service import database as database
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_prune_operations import (
    SkillPruneOperation,
    SkillPruneOperationDeletion,
)
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.repositories.skill_prune_operations import SkillPruneOperationRepository
from agent_remote_server.repositories.skill_retention import SkillRetentionRepository
from agent_remote_server.schemas.skill_prune import (
    PruneCommand,
    PrunePreviewPage,
    PrunePreviewRequest,
)
from agent_remote_server.schemas.skill_prune_rows import PruneDisclosure
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.gc.worker import SkillContentDeletionWorker
from agent_remote_server.services.skills.prune_commands import SkillPruneCommandService
from agent_remote_server.services.skills.prune_commands.queries import PruneQueries
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


def service(session: AsyncSession, root: Path, secret: str) -> SkillPruneCommandService:
    """
    用独立请求事务构造公开命令服务。

    :param session (AsyncSession): 当前事务
    :param root (Path): 测试私有卷父目录
    :param secret (str): 本用例随机签名密钥
    :return SkillPruneCommandService: 完整公开服务
    """
    return SkillPruneCommandService(
        session, PrivateObjectStore(root / "objects"), SkillStoragePolicy(), secret
    )


async def full_preview(
    state: RuntimeHarness,
    root: Path,
    secret: str,
    *,
    limit: int = 3,
) -> tuple[PrunePreviewPage, tuple[PruneDisclosure, ...]]:
    """
    模拟客户端逐页遍历，逐次提交只读请求并验证没有隐藏写入。

    :param state (RuntimeHarness): 已授权账户
    :param root (Path): 私有卷父目录
    :param secret (str): 当前部署密钥
    :param limit (int): 每次请求条数
    :return tuple[PrunePreviewPage, tuple[PruneDisclosure, ...]]: 最后可确认页与全部原披露
    """
    request = PrunePreviewRequest(
        selector=SkillStateSelector(account_id=state.account, scope="account-directory"),
        all_unreferenced=True,
        limit=limit,
    )
    original = await persisted_content(state)
    rows: list[PruneDisclosure] = []
    first = None
    while True:
        async with state.database.begin() as session:
            page = await service(session, root, secret).preview(state.owner, request)
            assert not session.new and not session.dirty and not session.deleted
        assert page.offset == len(rows)
        assert len(page.model_dump_json().encode()) < 1 << 20
        rows.extend(page.rows)
        if first is None:
            first = page
        assert first.summary == page.summary and first.total == page.total
        if page.next_cursor is None:
            break
        assert page.confirmation is None
        request = request.model_copy(
            update={"selector": page.summary.binding.selector, "cursor": page.next_cursor}
        )
    assert len(rows) == page.total and page.confirmation is not None
    assert await persisted_content(state) == original
    return page, tuple(rows)


async def test_complete_preview_acceptance_and_content_independent_replay(
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    所有原明细可以遍历，实际删除后原回执仍可在密钥轮换和内容访问失败时重放。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 禁止后续保留图读取
    """
    await shared_directory(stopped, tmp_path)
    secret = secrets.token_hex(32)
    last, disclosed = await full_preview(stopped, tmp_path, secret)
    assert last.confirmation is not None
    request = PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation)
    assert len(request.model_dump_json().encode()) < 4096
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path, secret).execute(stopped.owner, request)
    actual_rows: list[PruneDisclosure] = []
    async with stopped.database.begin() as session:
        queries = PruneQueries(session)
        while True:
            page = await queries.entries(stopped.owner, receipt.operation_id, len(actual_rows), 2)
            actual_rows.extend(page.rows)
            if page.next_offset is None:
                break
        assert len(actual_rows) == len(disclosed)
        for original, saved in zip(disclosed, actual_rows, strict=True):
            if original.kind == "compaction":
                assert saved.kind == "compaction" and saved.replacement_id is not None
                saved = saved.model_copy(update={"replacement_id": None})
            assert original == saved
        progress = await queries.progress(stopped.owner, receipt.operation_id)
        assert progress.pending_file_bytes == receipt.summary.pending_file_bytes > 0
        task_ids = tuple(
            await session.scalars(
                select(SkillPruneOperationDeletion.deletion_id).where(
                    SkillPruneOperationDeletion.operation_id == receipt.operation_id,
                )
            )
        )
    worker = SkillContentDeletionWorker(stopped.database, PrivateObjectStore(tmp_path / "objects"))
    for task_id in task_ids:
        assert await worker.process(task_id) == "complete"

    async def forbidden(self: SkillRetentionRepository, user_id: UUID) -> None:
        """
        已受理回执路径不能因旧保留图损坏或文件卷不可达而失效。

        :param user_id (UUID): 当前用户
        """
        raise AssertionError("receipt replay must not load retention graph")

    monkeypatch.setattr(SkillRetentionRepository, "load", forbidden)
    impossible = tmp_path / "not-a-directory"
    impossible.write_text("blocked")
    before = await persisted_content(stopped)
    async with stopped.database.begin() as session:
        replay = await service(session, impossible, secrets.token_hex(32)).execute(
            stopped.owner, request
        )
        queries = PruneQueries(session)
        assert replay == receipt == await queries.by_key(stopped.owner, request.idempotency_key)
        assert receipt == await queries.by_id(stopped.owner, receipt.operation_id)
        assert (await queries.entries(stopped.owner, receipt.operation_id)).total == len(disclosed)
        progress = await queries.progress(stopped.owner, receipt.operation_id)
        assert progress.pending_tasks == progress.pending_file_bytes == 0
        assert progress.completed_tasks == len(task_ids)
        assert progress.deleted_file_bytes == receipt.summary.pending_file_bytes
        with pytest.raises(SkillContentError, match="another prune"):
            await service(session, impossible, secret).execute(
                stopped.owner, request.model_copy(update={"confirmation": "different"})
            )
        with pytest.raises(SkillContentError, match="not found"):
            await queries.by_id(uuid4(), receipt.operation_id)
    assert await persisted_content(stopped) == before


async def test_last_receipt_failure_rolls_back_all_actions_even_if_caller_commits(
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    明细和任务链接写完后失败也必须撤销头、历史、额度和新回执，允许原请求重试。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入最后阶段失败
    """
    await shared_directory(stopped, tmp_path)
    secret = secrets.token_hex(32)
    last, _ = await full_preview(stopped, tmp_path, secret, limit=100)
    assert last.confirmation is not None
    request = PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation)
    before = await persisted_content(stopped)
    save = SkillPruneOperationRepository.save
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        amounts = (
            usage.package_bytes,
            usage.state_bytes,
            usage.package_reserved,
            usage.state_reserved,
        )

    async def failed_save(
        self: SkillPruneOperationRepository,
        operation: SkillPruneOperation,
        entries: Iterable[dict[str, object]],
        deletion_ids: tuple[UUID, ...],
    ) -> None:
        """
        原持久化全部完成后抛错，证明不是只撤销首次插入。

        :param operation (SkillPruneOperation): 原受理
        :param entries (Iterable[dict[str, object]]): 全部明细
        :param deletion_ids (tuple[UUID, ...]): 新任务
        """
        await save(self, operation, entries, deletion_ids)
        raise RuntimeError("last receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(SkillPruneOperationRepository, "save", failed_save)
        async with stopped.database.begin() as session:
            with pytest.raises(RuntimeError, match="last receipt"):
                await service(session, tmp_path, secret).execute(stopped.owner, request)
    after = await persisted_content(stopped)
    assert tuple(row for row in after if row[0] != "skill_storage_usage") == tuple(
        row for row in before if row[0] != "skill_storage_usage"
    )
    async with stopped.database() as session:
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None
        assert (
            usage.package_bytes,
            usage.state_bytes,
            usage.package_reserved,
            usage.state_reserved,
        ) == amounts
    async with stopped.database.begin() as session:
        assert (
            await service(session, tmp_path, secret).execute(stopped.owner, request)
        ).status == "accepted"
