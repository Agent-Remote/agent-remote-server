"""
验证冲突重算隔离原尝试，以及计划、人工树和幂等回执的数据库归属。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionOperation,
    SkillResolutionPlan,
)
from agent_remote_server.models.skill_state import AccountSkillState
from agent_remote_server.services.skills.publication import SkillPublicationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def conflict(state: RuntimeHarness, root: Path) -> SkillPublication:
    """
    用两份真实会话输入建立未解决发布尝试。

    :param state (RuntimeHarness): 原始终态会话
    :param root (Path): 内容卷
    :return SkillPublication: 已保留完整输入的冲突
    """
    other = await new_session(state, root)
    first = await ingest(state, root, {"learning/SKILL.md": b"first"})
    second = await ingest(other, root, {"learning/SKILL.md": b"second"})
    await publish(state, root, first)
    result = await publish(other, root, second)
    assert result.status == "conflicted"
    return result


async def recompute(state: RuntimeHarness, root: Path, identity: UUID) -> SkillPublication:
    """
    用独立事务显式重算目标已变更的原始冲突。

    :param state (RuntimeHarness): 请求用户身份
    :param root (Path): 内容卷
    :param identity (UUID): 原始冲突身份
    :return SkillPublication: 新的完整结果
    """
    async with state.database.begin() as session:
        service = SkillPublicationService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        return await service.recompute(state.owner, identity)


async def test_recomputation_retains_old_input_and_uses_new_attempt_content_keys(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    新 head 与旧比较不同仍可以建立独立尝试，重试原 ID 返回同一替代结果。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    old = await conflict(stopped, tmp_path)
    later = await new_session(stopped, tmp_path)
    await publish(later, tmp_path, await ingest(later, tmp_path, {"learning/SKILL.md": b"third"}))
    new = await recompute(stopped, tmp_path, old.id)
    assert new.status == "conflicted" and new.attempt == 2 and new.id != old.id
    assert new.current_tree_digest != old.current_tree_digest
    assert (await recompute(stopped, tmp_path, old.id)).id == new.id
    async with stopped.database() as session:
        previous = await session.get(SkillPublication, old.id)
        assert previous is not None and previous.status == "superseded"
        assert previous.current_tree_digest == old.current_tree_digest
        assert previous.conflicts_json == old.conflicts_json


async def test_recomputation_after_reset_detaches_whole_input(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    epoch 变化不是普通竞争，旧冲突取代后只能整体归档。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    old = await conflict(stopped, tmp_path)
    async with stopped.database.begin() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None
        branch.epoch += 1
        head = branch.head_checkpoint_id
    new = await recompute(stopped, tmp_path, old.id)
    assert new.status == "detached" and new.reason == "state_epoch_changed" and new.attempt == 2
    async with stopped.database() as session:
        branch = await session.get(AccountSkillState, stopped.state)
        assert branch is not None and branch.head_checkpoint_id == head


async def test_recomputation_can_publish_when_current_now_matches_incoming(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    head 已变为相同完整结果时，重算按新比较发布而不是套用旧选择。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    old = await conflict(stopped, tmp_path)
    later = await new_session(stopped, tmp_path)
    await publish(later, tmp_path, await ingest(later, tmp_path, {"learning/SKILL.md": b"second"}))
    new = await recompute(stopped, tmp_path, old.id)
    assert new.status == "published" and new.result_checkpoint_id is not None


async def test_plan_rejects_wrong_account_and_negative_revision(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    真实数据库外键和检查约束拒绝跨账户计划以及非法版本。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await conflict(stopped, tmp_path)
    for account, revision in ((uuid4(), 0), (stopped.account, -1)):
        with pytest.raises(IntegrityError):
            async with stopped.database.begin() as session:
                session.add(
                    SkillResolutionPlan(
                        publication_id=publication.id,
                        user_id=stopped.owner,
                        account_id=account,
                        revision=revision,
                    )
                )


async def test_choice_roots_custom_content_and_enforces_method_shape(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    人工内容选择必须引用同用户完整状态树，普通侧不能携带内容指针。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await conflict(stopped, tmp_path)
    async with stopped.database.begin() as session:
        session.add(
            SkillResolutionPlan(
                publication_id=publication.id,
                user_id=stopped.owner,
                account_id=stopped.account,
            )
        )
    for kind, digest in (
        ("file", None),
        ("current", publication.current_tree_digest),
        ("file", "a" * 64),
    ):
        with pytest.raises(IntegrityError):
            async with stopped.database.begin() as session:
                session.add(
                    SkillResolutionChoice(
                        publication_id=publication.id,
                        selector_key="test",
                        user_id=stopped.owner,
                        account_id=stopped.account,
                        path="learning/SKILL.md",
                        kind=kind,
                        tree_digest=digest,
                    )
                )
    async with stopped.database.begin() as session:
        session.add(
            SkillResolutionChoice(
                publication_id=publication.id,
                selector_key="whole",
                user_id=stopped.owner,
                account_id=stopped.account,
                kind="directory",
                tree_digest=publication.current_tree_digest,
            )
        )


async def test_resolution_operation_key_cannot_be_reused(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    即使另一操作行声明相同结果，用户幂等键也只能绑定一次原始命令。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await conflict(stopped, tmp_path)
    key = str(uuid4())
    async with stopped.database.begin() as session:
        session.add(
            SkillResolutionOperation(
                publication_id=publication.id,
                user_id=stopped.owner,
                account_id=stopped.account,
                idempotency_key=key,
                request_digest="a" * 64,
                plan_revision=1,
                response_json={},
            )
        )
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            session.add(
                SkillResolutionOperation(
                    publication_id=publication.id,
                    user_id=stopped.owner,
                    account_id=stopped.account,
                    idempotency_key=key,
                    request_digest="b" * 64,
                    plan_revision=2,
                    response_json={},
                )
            )
