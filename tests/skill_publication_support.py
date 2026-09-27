"""
为并发会话发布测试构造真实预约、上传和私有树读取。
"""

import io
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from skill_runtime_support import RuntimeHarness
from test_skill_content_service import service as content_service
from test_skill_finalization import service as finalization_service
from test_skill_snapshots import reserve
from test_skill_storage import file_entry

from agent_remote_server.models import NodeTask, Session
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.schemas.skill_finalizations import SkillFinalizationRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.publication import SkillPublicationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def new_session(state: RuntimeHarness, root: Path) -> RuntimeHarness:
    """
    在同账户创建另一份独立精确快照并模拟工具终止。

    :param state (RuntimeHarness): 原始账户与会话
    :param root (Path): 内容卷
    :return RuntimeHarness: 新会话身份及其固定快照
    """
    other = replace(state, session=uuid4(), task=uuid4())
    async with state.database.begin() as session:
        original = await session.get(Session, state.session)
        assert original is not None
        session.add(
            Session(
                id=other.session,
                tool_type=original.tool_type,
                user_id=state.owner,
                tool_account_id=state.account,
                workspace_id=original.workspace_id,
                node_id=state.node,
                project_key=str(other.session),
                status="starting",
                runtime_backend="native",
            )
        )
        session.add(
            NodeTask(
                id=other.task,
                node_id=state.node,
                task_id=str(other.task),
                task_type="create_tool_session",
                status="pending",
                payload={
                    "session_id": str(other.session),
                    "user_id": str(state.owner),
                    "tool_account_id": str(state.account),
                    "runtime_backend": "native",
                },
            )
        )
    other.snapshot = (await reserve(other, root)).id
    async with state.database.begin() as session:
        row = await session.get(Session, other.session)
        assert row is not None
        row.status = "stopped"
    return other


async def baseline(state: RuntimeHarness, root: Path) -> SkillTreeManifest:
    """
    从原始精确快照读实际物化的完整基线。

    :param state (RuntimeHarness): 会话身份
    :param root (Path): 内容卷
    :return SkillTreeManifest: 不受当前 head 改动影响的原始目录
    """
    async with state.database() as session:
        snapshot = await session.get(SessionSkillSnapshot, state.snapshot)
        assert snapshot is not None
        return await content_service(session, root).read_tree(
            state.owner, "state", snapshot.tree_digest
        )


async def ingest(
    state: RuntimeHarness,
    root: Path,
    changes: dict[str, bytes | None],
    *,
    unclean: bool = False,
    links: dict[str, str] | None = None,
) -> UUID:
    """
    相对会话基线生成完整输入，通过真实节点内容流程保存。

    :param state (RuntimeHarness): 会话身份
    :param root (Path): 内容卷
    :param changes (dict[str, bytes | None]): 普通文件替换、新增或子树删除
    :param unclean (bool): 不可变异常终止分类
    :param links (dict[str, str] | None): 要创建或替换的相对链接
    :return UUID: 完整收尾身份
    """
    entries = {entry.path: entry for entry in (await baseline(state, root)).entries}
    for path, data in changes.items():
        if data is None:
            entries = {
                key: entry
                for key, entry in entries.items()
                if key != path and not key.startswith(path + "/")
            }
            continue
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            entries.setdefault(parent, SkillTreeEntry(path=parent, kind="directory", mode=0o755))
        entries[path] = file_entry(data, path=path)
    for path, target in (links or {}).items():
        entries[path] = SkillTreeEntry(path=path, kind="symlink", mode=0o777, target=target)
    manifest = SkillTreeManifest(
        entries=tuple(sorted(entries.values(), key=lambda entry: entry.path))
    )
    payload = SkillFinalizationRequest(
        session_id=state.session,
        idempotency_key=str(uuid4()),
        manifest=manifest,
        unclean=unclean,
    )
    async with state.database.begin() as session:
        service = finalization_service(session, root)
        receipt = await service.begin(state.node, state.snapshot, payload)
        for path, data in changes.items():
            if data is not None:
                await service.put_file(
                    state.node,
                    receipt.id,
                    receipt.upload_id,
                    entries[path].sha256,
                    io.BytesIO(data),
                )
        await service.complete(state.node, receipt.id, receipt.upload_id)
        return receipt.id


async def publish(state: RuntimeHarness, root: Path, finalization_id: UUID) -> SkillPublication:
    """
    通过独立请求事务发布并提交结果。

    :param state (RuntimeHarness): 原始用户身份
    :param root (Path): 内容卷
    :param finalization_id (UUID): 已保存收尾
    :return SkillPublication: 已提交的完整发布结果
    """
    async with state.database.begin() as session:
        return await SkillPublicationService(
            session, PrivateObjectStore(root / "objects"), SkillStoragePolicy()
        ).publish(state.owner, finalization_id)


async def directory_tree(state: RuntimeHarness, root: Path) -> SkillTreeManifest:
    """
    读取当前已发布完整目录，而非某个未解决输入。

    :param state (RuntimeHarness): 账户身份
    :param root (Path): 内容卷
    :return SkillTreeManifest: 当前权威目录树
    """
    async with state.database() as session:
        directory = await session.get(AccountSkillDirectoryState, state.account)
        assert directory is not None and directory.head_checkpoint_id is not None
        checkpoint = await session.get(SkillCheckpoint, directory.head_checkpoint_id)
        assert checkpoint is not None and checkpoint.tree_digest is not None
        return await content_service(session, root).read_tree(
            state.owner, "state", checkpoint.tree_digest
        )
