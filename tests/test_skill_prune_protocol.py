"""
验证完整计划流式身份和签名分页边界，不能把游标提升为可执行确认。
"""

import base64
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_directory_compaction import shared_directory
from test_skill_finalization import stopped as stopped
from test_skill_prune_candidates import candidate_plan
from test_skill_snapshots import prepared as prepared

from agent_remote_server.schemas.skill_prune import PruneBinding
from agent_remote_server.schemas.skill_state_commands import SkillStateSelector
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.prune_commands.digest import digest
from agent_remote_server.services.skills.prune_commands.disclosure import disclosures, summary
from agent_remote_server.services.skills.prune_commands.tokens import PruneEnvelope, PruneTokens


def test_canonical_digest_preserves_types_order_and_utc() -> None:
    """
    映射和集合顺序不产生虚假变化，但字段分界、顺序、类型及微秒变化都不能混淆。
    """
    assert digest({"a": {"x", "y"}, "b": 2}) == digest({"b": 2, "a": {"y", "x"}})
    timestamp = datetime.now(UTC)
    assert digest(timestamp) == digest(timestamp.astimezone(timezone(timedelta(hours=8))))
    assert digest(timestamp) == digest(timestamp.replace(tzinfo=None))
    values: tuple[object, ...] = (
        True,
        1,
        "1",
        ("ab", "c"),
        ("a", "bc"),
        ["a", "bc"],
        ("bc", "a"),
        timestamp,
        timestamp + timedelta(microseconds=1),
    )
    assert len({digest(value) for value in values}) == len(values)
    with pytest.raises(TypeError, match="unsupported"):
        digest(object())


async def test_complete_plan_digest_binds_protection_claims_and_manifests(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    原计划可稳定重建，隐藏保护图、归属生命周期和完整清单都参与最终确认摘要。

    :param stopped (RuntimeHarness): 原始账户
    :param tmp_path (Path): 私有卷
    """
    await shared_directory(stopped, tmp_path)
    plan = await candidate_plan(stopped, tmp_path)
    again = await candidate_plan(stopped, tmp_path, cutoff=plan.cutoff)
    assert digest(plan) == digest(again)
    assert digest(plan) != digest(replace(plan, library_generation=plan.library_generation + 1))
    assert digest(plan) != digest(replace(plan, claims=((uuid4(), "*", "tree", "a" * 64),)))
    assert digest(plan) != digest(replace(plan, before=replace(plan.before, roots={})))
    assert plan.compaction is not None
    directory = plan.compaction.directories[0]
    assert directory.original.entries
    altered = replace(directory, original=directory.original.model_copy(update={"entries": ()}))
    changed = replace(plan.compaction, directories=(altered, *plan.compaction.directories[1:]))
    assert digest(plan) != digest(replace(plan, compaction=changed))
    rows = tuple(disclosures(plan))
    assert (
        sum(row.kind == "history" and row.selected for row in rows) == summary(plan).history_losses
    )
    assert plan.candidates is not None
    assert sum(row.kind == "history" for row in rows) == len(plan.candidates.entries)
    assert sum(row.kind == "dependency" for row in rows) == len(plan.candidates.dependencies)


def test_signed_stage_owner_and_each_bound_field_are_enforced() -> None:
    """
    即使知道明文结构也不能修改分页位置或确认范围，签名仍不允许跨用户和阶段。
    """
    owner = uuid4()
    tokens = PruneTokens(secrets.token_hex(32))
    envelope = PruneEnvelope(
        user_id=owner,
        binding=PruneBinding(
            selector=SkillStateSelector(account_id=uuid4(), scope="account-directory"),
            cutoff=datetime.now(UTC),
            all_unreferenced=True,
            plan_digest="a" * 64,
        ),
        offset=1,
        total=2,
        stage="page",
    )
    signed = tokens.sign(envelope)
    assert tokens.verify(signed, owner, "page") == envelope
    contexts: tuple[tuple[UUID, Literal["page", "confirm"]], ...] = (
        (uuid4(), "page"),
        (owner, "confirm"),
    )
    for user, stage in contexts:
        with pytest.raises(SkillContentError, match="invalid prune"):
            tokens.verify(signed, user, stage)
    payload, signature = signed.split(".")
    for field, value in (
        ("offset", 2),
        ("total", 1),
        ("stage", "confirm"),
        ("user_id", str(uuid4())),
    ):
        changed = json.loads(base64.urlsafe_b64decode(payload))
        changed[field] = value
        tampered = base64.urlsafe_b64encode(json.dumps(changed).encode()).decode() + "." + signature
        with pytest.raises(SkillContentError, match="invalid prune"):
            tokens.verify(tampered, owner, "page")
    with pytest.raises(SkillContentError, match="invalid prune"):
        tokens.verify(
            tokens.sign(envelope.model_copy(update={"stage": "confirm"})), owner, "confirm"
        )
    for invalid in ("", "x" * 4097, signed + ".", "中文.确认", payload + ".invalid"):
        with pytest.raises(SkillContentError, match="invalid prune"):
            tokens.verify(invalid, owner, "page")
