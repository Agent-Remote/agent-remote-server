"""
验证真实事实变化使所有旧分页和确认失效，以及大规模损失的有界传输。
"""

import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_history_planner import add_backed_history
from test_skill_library import LibraryHarness
from test_skill_prune_commands import full_preview, service
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_prune import PruneCommand, PrunePreviewRequest
from agent_remote_server.schemas.skill_prune_rows import PruneDisclosure
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.prune_commands.queries import PruneQueries


@pytest.mark.parametrize("change", ["pin", "upload", "consumer"])
async def test_changed_facts_reject_original_page_and_confirmation(
    stopped: RuntimeHarness,
    tmp_path: Path,
    change: str,
) -> None:
    """
    新 pin、零预留上传和截止后的消费者都使整份旧计划失效，不能静默减少清理目标。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    :param change (str): 确认前出现的新事实
    """
    old, _ = await shared_directory(stopped, tmp_path)
    secret = secrets.token_hex(32)
    last, _ = await full_preview(stopped, tmp_path, secret, limit=100)
    assert last.confirmation is not None
    request = PrunePreviewRequest(
        selector=last.summary.binding.selector, all_unreferenced=True, limit=1
    )
    async with stopped.database.begin() as session:
        first = await service(session, tmp_path, secret).preview(stopped.owner, request)
    assert first.next_cursor is not None
    if change == "consumer":
        await add_backed_history(stopped, old, 1)
    elif change == "upload":
        async with stopped.database.begin() as session:
            lease = await content_service(session, tmp_path).begin(
                stopped.owner,
                str(uuid4()),
                SkillTreeManifest(entries=(file_entry(b"historical", path="memory"),)),
                "state",
            )
            assert lease.reserved_bytes == 0
    else:
        async with stopped.database() as session:
            revision = await session.scalar(
                select(AccountSkillState.base_revision_id)
                .join(SkillCheckpoint, SkillCheckpoint.state_id == AccountSkillState.id)
                .where(SkillCheckpoint.id == old)
            )
        assert revision is not None
        library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
        await library.execute(
            SkillRuleRequest(
                command="pin",
                skill="learning",
                revision=str(revision),
                scope=SkillScope(account_id=stopped.account),
                expected_generation=await library.generation(),
                idempotency_key=str(uuid4()),
            )
        )
    async with stopped.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await service(session, tmp_path, secret).preview(
                stopped.owner, request.model_copy(update={"cursor": first.next_cursor})
            )
        assert error.value.code == "HEAD_CHANGED"
        with pytest.raises(SkillContentError) as error:
            await service(session, tmp_path, secret).execute(
                stopped.owner,
                PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation),
            )
        assert error.value.code == "HEAD_CHANGED"


async def test_large_disclosure_and_receipt_are_complete_but_request_stays_small(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    超过一千个关联损失全部可展示和恢复，原请求大小不随历史数量增长。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    """
    old, _ = await shared_directory(stopped, tmp_path)
    identities = await add_backed_history(stopped, old, 1001)
    secret = secrets.token_hex(32)
    last, rows = await full_preview(stopped, tmp_path, secret, limit=100)
    assert last.confirmation is not None
    selected = {row.history.id for row in rows if row.kind == "history" and row.selected}
    assert set(identities) <= selected and last.summary.history_losses > 1000
    request = PruneCommand(idempotency_key=str(uuid4()), confirmation=last.confirmation)
    assert len(request.model_dump_json().encode()) < 4096
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path, secret).execute(stopped.owner, request)
    recovered: list[PruneDisclosure] = []
    async with stopped.database() as session:
        while True:
            page = await PruneQueries(session).entries(
                stopped.owner, receipt.operation_id, len(recovered)
            )
            assert len(page.model_dump_json().encode()) < 1 << 20
            recovered.extend(page.rows)
            if page.next_offset is None:
                break
    assert len(recovered) == len(rows) == receipt.disclosure_rows
    assert {
        row.history.id for row in recovered if row.kind == "history" and row.selected
    } == selected
