"""
验证用户计划的原子保存、幂等竞争、预览和完整发布边界。
"""

import asyncio
import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import baseline, directory_tree, ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_directory_merge import directory
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_resolution import (
    SkillResolutionOperation,
    SkillResolutionPlan,
)
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, AccountSkillState
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_conflicts import SkillResolutionView
from agent_remote_server.schemas.skill_resolution import (
    SkillResolutionChoice,
    SkillResolutionRequest,
)
from agent_remote_server.services.skills.conflicts import SkillConflictService
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.resolution import SkillResolutionService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def pending(state: RuntimeHarness, root: Path) -> SkillPublication:
    """
    两个普通路径各自发生双方分歧，适合验证逐步计划。

    :param state (RuntimeHarness): 原始终态会话
    :param root (Path): 内容卷
    :return SkillPublication: 完整未解决尝试
    """
    other = await new_session(state, root)
    first = await ingest(state, root, {"learning/one": b"left1", "learning/two": b"left2"})
    second = await ingest(other, root, {"learning/one": b"right1", "learning/two": b"right2"})
    await publish(state, root, first)
    result = await publish(other, root, second)
    assert result.status == "conflicted"
    return result


async def choose(
    state: RuntimeHarness,
    root: Path,
    publication: SkillPublication,
    choice: SkillResolutionChoice,
    *,
    revision: int = 0,
    key: str | None = None,
    dry_run: bool = False,
) -> SkillResolutionView:
    """
    在独立事务提交一次明确用户选择。

    :param state (RuntimeHarness): 请求用户身份
    :param root (Path): 内容卷
    :param publication (SkillPublication): 已保存冲突
    :param choice (SkillResolutionChoice): 本次选择
    :param revision (int): 预期计划版本
    :param key (str | None): 可复用的命令键
    :param dry_run (bool): 是否只预览
    :return SkillResolutionView: 已保存或预览结果
    """
    async with state.database.begin() as session:
        service = SkillResolutionService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        )
        return await service.execute(
            state.owner,
            publication.id,
            SkillResolutionRequest(
                idempotency_key=key or str(uuid4()),
                expected_revision=revision,
                choice=choice,
                dry_run=dry_run,
            ),
        )


async def upload_tree(state: RuntimeHarness, root: Path, files: dict[str, bytes]) -> str:
    """
    为人工结果通过真实完整状态上传创建用户私有树。

    :param state (RuntimeHarness): 上传用户身份
    :param root (Path): 内容卷
    :param files (dict[str, bytes]): 相对路径及实际字节
    :return str: 已提交状态树摘要
    """
    tree = directory(files)
    async with state.database.begin() as session:
        service = content_service(session, root)
        upload = await service.begin(state.owner, str(uuid4()), tree, "account_directory")
        for entry in tree.entries:
            if entry.kind == "file":
                await service.put_file(
                    state.owner, upload.id, entry.sha256, io.BytesIO(files[entry.path])
                )
        return (await service.complete(state.owner, upload.id)).digest


async def test_partial_plan_then_atomic_publish_and_immutable_retry(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    第一次只保存选择，第二次才发布；之后重试仍返回第一次原始受理结果。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await pending(stopped, tmp_path)
    original = await directory_tree(stopped, tmp_path)
    key = str(uuid4())
    first_choice = SkillResolutionChoice(path="learning/one", use="current")
    first = await choose(stopped, tmp_path, publication, first_choice, key=key)
    assert first.status == "pending" and first.plan_revision == 1 and not first.ready
    assert (await directory_tree(stopped, tmp_path)) == original
    async with stopped.database() as session:
        info = await SkillConflictService(
            session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
        ).info(stopped.owner, publication.id)
        assert info.plan_revision == 1 and info.choices == [first_choice]
        assert (
            info.base.source == "session_snapshot"
            and info.current.source == "publication_comparison"
        )
    second = await choose(
        stopped,
        tmp_path,
        publication,
        SkillResolutionChoice(path="learning/two", use="incoming"),
        revision=1,
    )
    assert second.status == "published" and second.plan_revision == 2 and second.ready
    assert (await choose(stopped, tmp_path, publication, first_choice, key=key)) == first
    with pytest.raises(SkillContentError) as error:
        await choose(stopped, tmp_path, publication, SkillResolutionChoice(use="current"), key=key)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    tree = await directory_tree(stopped, tmp_path)
    expected = directory({"learning/one": b"left1", "learning/two": b"right2"})
    expected_files = {
        entry.path: entry.sha256 for entry in expected.entries if entry.kind == "file"
    }
    assert all(
        entry.sha256 == expected_files[entry.path]
        for entry in tree.entries
        if entry.path in expected_files
    )
    fresh = await new_session(stopped, tmp_path)
    assert (await baseline(fresh, tmp_path)) == tree


async def test_dry_run_has_no_plan_operation_or_head_writes(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    完整可发布预览也不保存计划、候选或回执，不推进任何 head。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await pending(stopped, tmp_path)
    before = await directory_tree(stopped, tmp_path)
    view = await choose(
        stopped, tmp_path, publication, SkillResolutionChoice(use="incoming"), dry_run=True
    )
    assert view.status == "preview" and view.ready and view.plan_revision == 0
    assert view.operation_id is None and view.result_checkpoint_id is None
    assert await directory_tree(stopped, tmp_path) == before
    async with stopped.database() as session:
        assert await session.get(SkillResolutionPlan, publication.id) is None
        assert not (
            await session.scalars(
                select(SkillResolutionOperation).where(
                    SkillResolutionOperation.publication_id == publication.id
                )
            )
        ).all()


async def test_plan_revision_serializes_concurrent_steps(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    两个同版本请求只有一个保存，另一个必须刷新计划而非覆盖已保存选择。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await pending(stopped, tmp_path)
    results = await asyncio.gather(
        choose(
            stopped,
            tmp_path,
            publication,
            SkillResolutionChoice(path="learning/one", use="current"),
        ),
        choose(
            stopped,
            tmp_path,
            publication,
            SkillResolutionChoice(path="learning/two", use="incoming"),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, SkillResolutionView) for item in results) == 1
    errors = [item for item in results if isinstance(item, SkillContentError)]
    assert len(errors) == 1 and errors[0].code == "PLAN_REVISION_CONFLICT"


async def test_custom_file_and_full_directory_are_validated_and_published(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    人工文件只替换指定冲突，完整目录选择替换旧计划并支持新增本地技能。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    publication = await pending(stopped, tmp_path)
    digest = await upload_tree(stopped, tmp_path, {"content": b"manual"})
    partial = await choose(
        stopped,
        tmp_path,
        publication,
        SkillResolutionChoice(path="learning/one", file_tree_digest=digest),
    )
    assert partial.status == "pending"
    replacement = await upload_tree(
        stopped, tmp_path, {"learning/SKILL.md": b"fixed", "notes/SKILL.md": b"new skill"}
    )
    result = await choose(
        stopped,
        tmp_path,
        publication,
        SkillResolutionChoice(directory_tree_digest=replacement),
        revision=1,
    )
    assert result.status == "published" and len(result.choices) == 1
    assert result.result_tree_digest == replacement
    fresh = await new_session(stopped, tmp_path)
    assert {entry.path for entry in (await baseline(fresh, tmp_path)).entries} == {
        "learning",
        "learning/SKILL.md",
        "notes",
        "notes/SKILL.md",
    }


@pytest.mark.parametrize("reset", [False, True])
async def test_stale_target_preview_does_not_write_and_commit_recomputes_without_choices(
    stopped: RuntimeHarness,
    tmp_path: Path,
    reset: bool,
) -> None:
    """
    旧计划不写入新目标，预览无替代尝试，真正提交只重算或整体归档。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    :param reset (bool): 是否推进状态纪元而非普通新 head
    """
    publication = await pending(stopped, tmp_path)
    await choose(
        stopped, tmp_path, publication, SkillResolutionChoice(path="learning/one", use="current")
    )
    if reset:
        async with stopped.database.begin() as session:
            branch = await session.get(AccountSkillState, stopped.state)
            assert branch is not None
            branch.epoch += 1
    else:
        later = await new_session(stopped, tmp_path)
        await publish(later, tmp_path, await ingest(later, tmp_path, {"learning/one": b"third"}))
    selection = SkillResolutionChoice(path="learning/two", use="incoming")
    preview = await choose(stopped, tmp_path, publication, selection, revision=1, dry_run=True)
    assert not preview.ready and preview.replacement_id is None and preview.stale_reason is not None
    async with stopped.database() as session:
        old = await session.get(SkillPublication, publication.id)
        assert old is not None and old.status == "conflicted"
    result = await choose(stopped, tmp_path, publication, selection, revision=1)
    assert result.status == "superseded" and result.replacement_id is not None
    async with stopped.database() as session:
        new = await session.get(SkillPublication, result.replacement_id)
        assert new is not None and new.status == ("detached" if reset else "conflicted")
        assert await session.get(SkillResolutionPlan, new.id) is None


async def test_late_cas_failure_rolls_back_choices_and_receipt(
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    最后 head 条件失败时，计划版本和人工引用也不能留下部分成功。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 控制最终 CAS 失败
    """

    async def reject(
        self: SkillPublicationRepository, directory: AccountSkillDirectoryState, checkpoint_id: UUID
    ) -> bool:
        """
        模拟目录 head 已变化。

        :param directory (AccountSkillDirectoryState): 旧目标
        :param checkpoint_id (UUID): 待发布检查点
        :return bool: 比较交换未命中
        """
        return False

    publication = await pending(stopped, tmp_path)
    before = await directory_tree(stopped, tmp_path)
    monkeypatch.setattr(SkillPublicationRepository, "advance_directory", reject)
    with pytest.raises(SkillContentError, match="directory changed"):
        await choose(stopped, tmp_path, publication, SkillResolutionChoice(use="incoming"))
    assert await directory_tree(stopped, tmp_path) == before
    async with stopped.database() as session:
        assert await session.get(SkillResolutionPlan, publication.id) is None
        assert not (
            await session.scalars(
                select(SkillResolutionOperation).where(
                    SkillResolutionOperation.publication_id == publication.id
                )
            )
        ).all()


async def test_source_conflict_identical_bytes_still_require_current_identity(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    内容相同不能推断来源相同，只有明确选择现有侧才能保留其身份。

    :param stopped (RuntimeHarness): 原始会话
    :param tmp_path (Path): 内容卷
    """
    other = await new_session(stopped, tmp_path)
    first = await ingest(stopped, tmp_path, {"notes/SKILL.md": b"same"})
    second = await ingest(other, tmp_path, {"notes/SKILL.md": b"same"})
    await publish(stopped, tmp_path, first)
    publication = await publish(other, tmp_path, second)
    with pytest.raises(SkillContentError) as error:
        await choose(stopped, tmp_path, publication, SkillResolutionChoice(use="incoming"))
    assert error.value.code == "SKILL_SOURCE_CONFLICT"
    result = await choose(
        stopped, tmp_path, publication, SkillResolutionChoice(path="notes", use="current")
    )
    assert result.status == "published"
