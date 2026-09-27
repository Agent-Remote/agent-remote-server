"""
验证收尾输入不可变、续期隔离、完整持久化与异常状态保留。
"""

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve
from test_skill_storage import file_entry

from agent_remote_server.models import Session
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStorageUsage
from agent_remote_server.schemas.skill_finalizations import (
    SkillFinalizationRequest,
    SkillFinalizationView,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization import SkillFinalizationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


@pytest.fixture
async def stopped(prepared: RuntimeHarness, tmp_path: Path) -> RuntimeHarness:
    """
    从真实预约得到精确快照，再模拟节点已报告无写入的终态。

    :param prepared (RuntimeHarness): 准备身份
    :param tmp_path (Path): 私有内容卷
    :return RuntimeHarness: 已停止且有保留启动快照的身份
    """
    snapshot = await reserve(prepared, tmp_path)
    prepared.snapshot = snapshot.id
    async with prepared.database.begin() as session:
        await session.execute(
            update(Session).where(Session.id == prepared.session).values(status="stopped")
        )
        await session.execute(
            update(SessionSkillSnapshot)
            .where(SessionSkillSnapshot.id == snapshot.id)
            .values(status="started")
        )
    return prepared


def service(
    session: AsyncSession, root: Path, policy: SkillStoragePolicy | None = None
) -> SkillFinalizationService:
    """
    每次请求重建无内存权威的服务。

    :param session (AsyncSession): 请求事务
    :param root (Path): 内容卷
    :param policy (SkillStoragePolicy | None): 可选测试配额
    :return SkillFinalizationService: 收尾服务
    """
    return SkillFinalizationService(
        session, PrivateObjectStore(root / "objects"), policy or SkillStoragePolicy()
    )


def request(
    state: RuntimeHarness,
    content: bytes = b"---\nname: learning\n---\nlearned",
    *,
    unclean: bool = False,
) -> SkillFinalizationRequest:
    """
    创建包含学习结果的完整目录输入。

    :param state (RuntimeHarness): 精确快照身份
    :param content (bytes): 实际说明文件或任意运行字节
    :param unclean (bool): 原始异常退出标记
    :return SkillFinalizationRequest: 完整不可变提交
    """
    manifest = SkillTreeManifest(
        entries=(
            SkillTreeEntry(path="learning", kind="directory", mode=0o755),
            file_entry(content, path="learning/SKILL.md"),
        )
    )
    return SkillFinalizationRequest(
        session_id=state.session, idempotency_key=str(uuid4()), manifest=manifest, unclean=unclean
    )


async def test_full_persistence_is_atomic_and_does_not_publish(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    每文件成功不代表完整保存；完成重试复用原检查点且不推进 head。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    payload = request(stopped)
    async with stopped.database.begin() as session:
        first = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).complete(stopped.node, first.id, first.upload_id)
    assert error.value.code == "CONTENT_INCOMPLETE"
    async with stopped.database.begin() as session:
        await service(session, tmp_path).put_file(
            stopped.node,
            first.id,
            first.upload_id,
            payload.manifest.entries[1].sha256,
            io.BytesIO(b"---\nname: learning\n---\nlearned"),
        )
    async with stopped.database() as session:
        finalization = await session.get(SkillFinalization, first.id)
        assert finalization is not None and finalization.tree_digest is None
    async with stopped.database.begin() as session:
        done = await service(session, tmp_path).complete(stopped.node, first.id, first.upload_id)
    async with stopped.database.begin() as session:
        replay = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
        assert replay == done and done.status == "persisted" and done.checkpoint_id is not None
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None and directory.head_checkpoint_id == stopped.directory


@pytest.mark.parametrize("mutation", ["tree", "unclean", "key", "session"])
async def test_original_input_and_classification_cannot_change(
    stopped: RuntimeHarness, tmp_path: Path, mutation: str
) -> None:
    """
    相同快照只接受第一次完整输入，不能借新键替换或降级异常标记。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    :param mutation (str): 请求篡改方式
    """
    payload = request(stopped, unclean=True)
    async with stopped.database.begin() as session:
        await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
    changes: dict[str, dict[str, object]] = {
        "tree": {"manifest": SkillTreeManifest()},
        "unclean": {"unclean": False},
        "key": {"idempotency_key": str(uuid4())},
        "session": {"session_id": uuid4()},
    }
    changed = payload.model_copy(update=changes[mutation])
    with pytest.raises(SkillContentError):
        async with stopped.database.begin() as session:
            await service(session, tmp_path).begin(stopped.node, stopped.snapshot, changed)


async def test_expired_transfer_renews_only_lease_and_rejects_old_attempt(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    上传租约过期后可继续保存本地唯一副本，旧尝试不能写入新租约。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    payload = request(stopped)
    async with stopped.database.begin() as session:
        first = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
        upload = await session.get(SkillContentUpload, first.upload_id)
        assert upload is not None
        upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with stopped.database.begin() as session:
        renewed = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
        assert (
            renewed.id == first.id
            and renewed.upload_id != first.upload_id
            and renewed.upload_attempt == 2
        )
        usage = await session.get(SkillStorageUsage, stopped.owner)
        assert usage is not None and usage.state_reserved == payload.manifest.total_bytes
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).prepare_file(
                stopped.node, first.id, first.upload_id, payload.manifest.entries[1].sha256
            )
    assert error.value.code == "UPLOAD_SUPERSEDED"


@pytest.mark.parametrize("content", [b"---\ninvalid: [\n", b"\x00SQLite state\xff"])
async def test_invalid_format_and_unclean_bytes_are_retained(
    stopped: RuntimeHarness, tmp_path: Path, content: bytes
) -> None:
    """
    不符合技能格式的异常退出输入仍保存完整字节，标记诊断而不丢弃。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    :param content (bytes): 文本格式损坏或二进制运行内容
    """
    payload = request(stopped, content, unclean=True)
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path)
        plan = await svc.begin(stopped.node, stopped.snapshot, payload)
        await svc.put_file(
            stopped.node,
            plan.id,
            plan.upload_id,
            payload.manifest.entries[1].sha256,
            io.BytesIO(content),
        )
        done = await svc.complete(stopped.node, plan.id, plan.upload_id)
        assert done.status == "persisted_unclean" and done.unclean
        members = (
            await session.scalars(
                select(SkillDirectoryMember).where(
                    SkillDirectoryMember.directory_checkpoint_id == done.checkpoint_id
                )
            )
        ).all()
        assert len(members) == 1
        item = await session.get(SkillCheckpoint, members[0].checkpoint_id)
        assert (
            item is not None
            and item.invalid_skill_format
            and item.tree_digest == done.incoming_digest
        )


@pytest.mark.parametrize("kind", ["item", "auxiliary", "new_skill"])
async def test_runtime_scope_quotas_are_independent(
    stopped: RuntimeHarness, tmp_path: Path, kind: str
) -> None:
    """
    完整目录未超额时仍要分别限制已知技能、新技能和根级辅助数据。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    :param kind (str): 超额范围
    """
    payload = request(stopped, b"too much state")
    if kind == "auxiliary":
        payload = payload.model_copy(
            update={
                "manifest": SkillTreeManifest(
                    entries=(file_entry(b"too much state", path="cache"),)
                )
            }
        )
    elif kind == "new_skill":
        payload = payload.model_copy(
            update={
                "manifest": SkillTreeManifest(
                    entries=(
                        SkillTreeEntry(path="new", kind="directory", mode=0o755),
                        file_entry(b"too much state", path="new/SKILL.md"),
                    )
                )
            }
        )
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path, SkillStoragePolicy(checkpoint_bytes=5)).begin(
                stopped.node, stopped.snapshot, payload
            )
    assert error.value.code == "QUOTA_EXCEEDED"
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(SkillFinalization).where(SkillFinalization.snapshot_id == stopped.snapshot)
            )
            is None
        )


async def test_live_session_and_wrong_node_cannot_submit(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    内容摘要和会话身份不能替代节点归属与停止前置条件。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    payload = request(stopped)
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).begin(uuid4(), stopped.snapshot, payload)
    assert error.value.code == "SNAPSHOT_NOT_FOUND"
    async with stopped.database.begin() as session:
        await session.execute(
            update(Session).where(Session.id == stopped.session).values(status="running")
        )
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
    assert error.value.code == "STATE_WRITERS_ACTIVE"


async def test_concurrent_begin_and_complete_are_idempotent(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    两个独立连接竞争受理和完成时只产生一个收尾和一份输入检查点。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    import asyncio

    payload = request(stopped, b"shared completion")

    async def begin() -> SkillFinalizationView:
        """
        在独立事务中提交同一请求。

        :return SkillFinalizationView: 持久化回执
        """
        async with stopped.database.begin() as session:
            return await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)

    first, second = await asyncio.gather(begin(), begin())
    assert first.id == second.id and first.upload_id == second.upload_id
    async with stopped.database.begin() as session:
        await service(session, tmp_path).put_file(
            stopped.node,
            first.id,
            first.upload_id,
            payload.manifest.entries[1].sha256,
            io.BytesIO(b"shared completion"),
        )

    async def complete() -> SkillFinalizationView:
        """
        在独立事务中竞争完整内容登记。

        :return SkillFinalizationView: 唯一完整检查点回执
        """
        async with stopped.database.begin() as session:
            return await service(session, tmp_path).complete(
                stopped.node, first.id, first.upload_id
            )

    left, right = await asyncio.gather(complete(), complete())
    assert left.checkpoint_id == right.checkpoint_id and left.status == right.status == "persisted"


async def test_completion_rollback_keeps_original_input_retryable(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    外层提交前崩溃不会留下假持久化回执或半份 checkpoint 成员。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    payload = request(stopped, b"rollback state")
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path)
        plan = await svc.begin(stopped.node, stopped.snapshot, payload)
        await svc.put_file(
            stopped.node,
            plan.id,
            plan.upload_id,
            payload.manifest.entries[1].sha256,
            io.BytesIO(b"rollback state"),
        )
    with pytest.raises(RuntimeError, match="before commit"):
        async with stopped.database.begin() as session:
            await service(session, tmp_path).complete(stopped.node, plan.id, plan.upload_id)
            raise RuntimeError("before commit")
    async with stopped.database.begin() as session:
        receipt = await service(session, tmp_path).get(stopped.node, plan.id)
        assert receipt.status == "upload_pending" and receipt.checkpoint_id is None
        assert receipt.upload_status == "staged"
        checkpoints = (
            await session.scalars(
                select(SkillCheckpoint).where(
                    SkillCheckpoint.source_session_reference_id == stopped.session
                )
            )
        ).all()
        assert not checkpoints
        completed = await service(session, tmp_path).complete(stopped.node, plan.id, plan.upload_id)
        assert completed.status == "persisted"


async def test_complete_deletion_is_retained_as_empty_directory(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    合法完整空清单表达全部删除，不把未上传文件误解释为删除。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    payload = request(stopped).model_copy(update={"manifest": SkillTreeManifest()})
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path)
        plan = await svc.begin(stopped.node, stopped.snapshot, payload)
        completed = await svc.complete(stopped.node, plan.id, plan.upload_id)
        assert completed.status == "persisted"
        members = (
            await session.scalars(
                select(SkillDirectoryMember).where(
                    SkillDirectoryMember.directory_checkpoint_id == completed.checkpoint_id
                )
            )
        ).all()
        assert not members


async def test_transfer_foreign_keys_reject_different_input_and_scope(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    数据库不允许传输引用借用其他摘要或安装包上传租约。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    from sqlalchemy.exc import IntegrityError
    from test_skill_content_service import service as content_service

    from agent_remote_server.models.skill_transfers import SkillFinalizationTransfer

    payload = request(stopped)
    async with stopped.database.begin() as session:
        plan = await service(session, tmp_path).begin(stopped.node, stopped.snapshot, payload)
        content = content_service(session, tmp_path)
        different = await content.begin(
            stopped.owner, str(uuid4()), SkillTreeManifest(), "account_directory"
        )
        package = await content.begin(stopped.owner, str(uuid4()), payload.manifest, "package")
    for upload_id in (different.id, package.id):
        with pytest.raises(IntegrityError):
            async with stopped.database.begin() as session:
                await session.execute(
                    update(SkillFinalizationTransfer)
                    .where(SkillFinalizationTransfer.finalization_id == plan.id)
                    .values(upload_id=upload_id)
                )


@pytest.mark.parametrize("valid", [False, True])
async def test_new_invalid_skill_directories_cannot_split_auxiliary_quota(
    stopped: RuntimeHarness, tmp_path: Path, valid: bool
) -> None:
    """
    新目录只有格式实际有效时独立计量，否则全部合并到根级辅助范围。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    :param valid (bool): 新说明文件是否有效
    """
    content = b"good" if valid else b"---\n"
    manifest = SkillTreeManifest(
        entries=(
            SkillTreeEntry(path="first", kind="directory", mode=0o755),
            file_entry(content, path="first/SKILL.md"),
            SkillTreeEntry(path="second", kind="directory", mode=0o755),
            file_entry(content, path="second/SKILL.md"),
        )
    )
    payload = request(stopped).model_copy(update={"manifest": manifest})
    policy = SkillStoragePolicy(checkpoint_bytes=5, package_file_bytes=1)
    async with stopped.database.begin() as session:
        svc = service(session, tmp_path, policy)
        plan = await svc.begin(stopped.node, stopped.snapshot, payload)
        await svc.put_file(
            stopped.node, plan.id, plan.upload_id, manifest.entries[1].sha256, io.BytesIO(content)
        )
    if valid:
        async with stopped.database.begin() as session:
            done = await service(session, tmp_path, policy).complete(
                stopped.node, plan.id, plan.upload_id
            )
            assert done.status == "persisted"
    else:
        with pytest.raises(SkillContentError) as error:
            async with stopped.database.begin() as session:
                await service(session, tmp_path, policy).complete(
                    stopped.node, plan.id, plan.upload_id
                )
        assert error.value.code == "QUOTA_EXCEEDED"
        async with stopped.database() as session:
            receipt = await session.get(SkillFinalization, plan.id)
            assert receipt is not None and receipt.tree_digest is None


async def test_new_invalid_names_cannot_split_auxiliary_quota(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    不满足本地身份名称规则的新目录始终按根级辅助数据计量。

    :param stopped (RuntimeHarness): 终态绑定
    :param tmp_path (Path): 内容卷
    """
    content = b"good"
    manifest = SkillTreeManifest(
        entries=(
            SkillTreeEntry(path="First", kind="directory", mode=0o755),
            file_entry(content, path="First/SKILL.md"),
            SkillTreeEntry(path="Second", kind="directory", mode=0o755),
            file_entry(content, path="Second/SKILL.md"),
        )
    )
    payload = request(stopped).model_copy(update={"manifest": manifest})
    with pytest.raises(SkillContentError) as error:
        async with stopped.database.begin() as session:
            await service(session, tmp_path, SkillStoragePolicy(checkpoint_bytes=5)).begin(
                stopped.node, stopped.snapshot, payload
            )
    assert error.value.code == "QUOTA_EXCEEDED"
