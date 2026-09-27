"""
验证首次目录接管的持久重试、完整内容发布和失败原子性。
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.db import Base
from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_local import AccountLocalSkill, AccountLocalSkillRevision
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStoredTree
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.skill_takeover import SkillTakeoverRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.schemas.skill_takeover import SkillTakeoverRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.manifest import manifest_digest


async def test_reservation_replays_after_restart_and_feature_disable(
    takeover: TakeoverHarness,
) -> None:
    """
    相同请求复用单个任务与纪元，不因之后开关关闭丢失回执。

    :param takeover (TakeoverHarness): 未接管账户
    """
    first = await takeover.reserve()
    takeover.settings = takeover.settings.model_copy(update={"skill_manager_enabled": False})
    replay = await takeover.reserve()
    assert first.id == replay.id and first.task_id == replay.task_id
    async with takeover.library.database() as session:
        directory = await session.get(AccountSkillDirectoryState, takeover.account)
        assert directory is not None and directory.mode == "migrating" and directory.epoch == 1
        assert directory.head_checkpoint_id is None
        tasks = (
            await session.scalars(select(NodeTask).where(NodeTask.node_id == takeover.node))
        ).all()
        assert len(tasks) == 1 and tasks[0].status == "pending"
        assert tasks[0].payload["takeover_id"] == str(first.id)
    with pytest.raises(SkillContentError) as changed:
        await takeover.reserve(epoch=1)
    assert changed.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("epoch", [0, 1])
async def test_second_key_cannot_replace_reserved_takeover(
    takeover: TakeoverHarness, epoch: int
) -> None:
    """
    新键不能创建第二个接管身份或绕过迁移模式。

    :param takeover (TakeoverHarness): 未接管账户
    :param epoch (int): 请求比较纪元
    """
    first = await takeover.reserve()
    with pytest.raises(SkillContentError) as error:
        await takeover.reserve(key="second", epoch=epoch)
    assert error.value.code == "HEAD_CHANGED"
    assert (await takeover.reserve()).id == first.id


async def test_mixed_tree_publishes_only_account_local_identities(
    takeover: TakeoverHarness,
) -> None:
    """
    手工技能、空目录、无效技能和跨入口链接全部保留，且不会安装到用户库。

    :param takeover (TakeoverHarness): 未接管账户
    """
    files = {
        "learning/SKILL.md": b"---\nname: learning\ndescription: Learn\n---\nmanual\n",
        "learning/state.db": b"\x00\xffstate",
        "broken/SKILL.md": b"---\nname: [broken metadata\n---\n",
        "root.txt": b"auxiliary",
    }
    manifest = tree(
        files,
        (
            SkillTreeEntry(path="empty", kind="directory", mode=0o700),
            SkillTreeEntry(
                path="learning/shared", kind="symlink", mode=0o777, target="../root.txt"
            ),
        ),
    )
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    capture = takeover.capture(receipt, manifest)
    receipt = await takeover.begin(receipt, capture)
    await takeover.transfer(receipt, files)
    done = await takeover.complete(receipt)
    assert done.status == "committed" and done.capture_digest == manifest_digest(manifest)
    async with takeover.library.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, takeover.account)
        assert directory is not None and directory.mode == "managed_v1"
        assert directory.head_checkpoint_id == done.checkpoint_id and directory.epoch == 1
        skills = (
            await session.scalars(
                select(AccountLocalSkill).where(AccountLocalSkill.account_id == takeover.account)
            )
        ).all()
        assert len(skills) == 1 and skills[0].name == "learning" and skills[0].status == "active"
        branch = await session.scalar(
            select(AccountSkillState).where(AccountSkillState.account_id == takeover.account)
        )
        assert branch is not None and branch.local_skill_id == skills[0].id
        assert branch.installation_id is None and branch.base_revision_id is None
        item = await session.get(SkillCheckpoint, branch.head_checkpoint_id)
        assert item is not None and item.backing_directory_id == done.checkpoint_id
        assert item.subtree_prefix == "learning" and item.state_epoch == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillInstallation)
                .where(SkillInstallation.user_id == takeover.library.owner)
            )
            == 0
        )
        assert (
            await takeover.service(session).context.content.read_tree(
                takeover.library.owner, "account_directory", done.capture_digest
            )
            == manifest
        )
        later = SkillCheckpoint(
            id=uuid4(),
            user_id=takeover.library.owner,
            account_id=takeover.account,
            scope="directory",
            directory_epoch=9,
            parent_id=done.checkpoint_id,
            content_digest=done.capture_digest,
            tree_digest=done.capture_digest,
        )
        session.add(later)
        await session.flush()
        directory.epoch, directory.head_checkpoint_id = 9, later.id
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None
        task.status, task.lease_until = "succeeded", None
    assert (await takeover.complete(receipt)).checkpoint_id == done.checkpoint_id
    assert (await takeover.begin(receipt, capture)).checkpoint_id == done.checkpoint_id
    async with takeover.library.database() as session:
        directory = await session.get(AccountSkillDirectoryState, takeover.account)
        assert directory is not None and directory.epoch == 9
        assert directory.head_checkpoint_id == later.id


async def test_missing_bytes_never_publish_partial_directory(takeover: TakeoverHarness) -> None:
    """
    声明完整清单不代表字节完整，遗漏文件时保留可重试上传而不切换模式。

    :param takeover (TakeoverHarness): 未接管账户
    """
    files = {"first": b"first", "second": b"second"}
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, {"first": b"first"})
    with pytest.raises(FileNotFoundError):
        await takeover.complete(receipt)
    async with takeover.library.database() as session:
        directory = await session.get(AccountSkillDirectoryState, takeover.account)
        assert directory is not None and directory.mode == "migrating"
        upload = await session.get(SkillContentUpload, receipt.upload_id)
        assert upload is not None and upload.status == "staged"
    await takeover.transfer(receipt, {"second": b"second"})
    assert (await takeover.complete(receipt)).status == "committed"


async def test_late_publish_failure_rolls_back_even_if_caller_commits(
    takeover: TakeoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    最终 CAS 失败回滚已建候选、分支、内容引用与上传完成状态，外层仍可提交。

    :param takeover (TakeoverHarness): 未接管账户
    :param monkeypatch (pytest.MonkeyPatch): 失败注入器
    """
    files = {"learning/SKILL.md": b"---\nname: learning\ndescription: Learn\n---\n"}
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    manifest = tree(files)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, manifest))
    await takeover.transfer(receipt, files)

    async def fail_publish(
        self: SkillTakeoverRepository, receipt: SkillAccountTakeover, checkpoint_id: UUID
    ) -> bool:
        """
        模拟最后目录比较交换失败。

        :param receipt (SkillAccountTakeover): 原始收据
        :param checkpoint_id (UUID): 已构建目录
        :return bool: 拒绝交换
        """
        return False

    with monkeypatch.context() as patch:
        patch.setattr(SkillTakeoverRepository, "publish", fail_publish)
        async with takeover.library.database.begin() as session:
            with pytest.raises(SkillContentError) as error:
                assert receipt.upload_id is not None
                await takeover.service(session).complete(
                    takeover.node, receipt.id, receipt.task_id, receipt.upload_id
                )
            assert error.value.code == "HEAD_CHANGED"
    async with takeover.library.database() as session:
        for model in (
            AccountLocalSkill,
            AccountLocalSkillRevision,
            AccountSkillState,
            SkillCheckpoint,
            SkillDirectoryMember,
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.account_id == takeover.account)
                )
                == 0
            )
        assert (
            await session.get(
                SkillStoredTree, (takeover.library.owner, "state", manifest_digest(manifest))
            )
            is None
        )
        upload = await session.get(SkillContentUpload, receipt.upload_id)
        assert upload is not None and upload.status == "staged"
        saved = await session.get(SkillAccountTakeover, receipt.id)
        assert saved is not None and saved.status == "uploading" and saved.checkpoint_id is None
    assert (await takeover.complete(receipt)).status == "committed"


async def test_reservation_failure_removes_directory_and_task(
    takeover: TakeoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    收据插入失败时任务与模式也回滚，不留下没有可恢复身份的迁移围栏。

    :param takeover (TakeoverHarness): 未接管账户
    :param monkeypatch (pytest.MonkeyPatch): 失败注入器
    """
    from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository

    original = SkillRuntimeRepository.add

    def fail_add(self: SkillRuntimeRepository, instance: Base) -> None:
        """
        在真实目录与任务写入之后注入异常。

        :param instance (Base): 待写模型
        """
        if isinstance(instance, SkillAccountTakeover):
            raise RuntimeError("receipt unavailable")
        original(self, instance)

    with monkeypatch.context() as patch:
        patch.setattr(SkillRuntimeRepository, "add", fail_add)
        async with takeover.library.database.begin() as session:
            with pytest.raises(RuntimeError, match="receipt unavailable"):
                await takeover.service(session).reserve(
                    takeover.library.owner,
                    takeover.account,
                    SkillTakeoverRequest(idempotency_key=str(uuid4()), expected_directory_epoch=0),
                )
    async with takeover.library.database() as session:
        assert await session.get(AccountSkillDirectoryState, takeover.account) is None
        assert (
            await session.scalar(
                select(func.count()).select_from(NodeTask).where(NodeTask.node_id == takeover.node)
            )
            == 0
        )
    assert (await takeover.reserve()).directory_epoch == 1


async def test_concurrent_retries_share_reservation_upload_and_commit(
    takeover: TakeoverHarness,
) -> None:
    """
    多条独立连接竞争同一接管时只保留一套任务、上传和权威检查点。

    :param takeover (TakeoverHarness): 未接管账户
    """
    first, second = await asyncio.gather(takeover.reserve(), takeover.reserve())
    assert first.id == second.id and first.task_id == second.task_id
    await takeover.lease(first)
    request = takeover.capture(first, tree({}))
    upload, repeated = await asyncio.gather(
        takeover.begin(first, request), takeover.begin(second, request)
    )
    assert upload.upload_id == repeated.upload_id and upload.upload_attempt == 1
    done, replay = await asyncio.gather(takeover.complete(upload), takeover.complete(repeated))
    assert (
        done.checkpoint_id == replay.checkpoint_id and done.status == replay.status == "committed"
    )
    async with takeover.library.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillCheckpoint)
                .where(SkillCheckpoint.account_id == takeover.account)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(NodeTask).where(NodeTask.node_id == takeover.node)
            )
            == 1
        )
