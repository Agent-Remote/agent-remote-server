"""
在显式新节点调度夹具上领取真实恢复快照，只导出当前租约授权的内容。
"""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from skill_restore_support import run_fixture_command
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_session_admission import ready

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, Session, User, Workspace
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.sessions import ToolSessionService
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.node_content import NodeSkillContentService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def verify_new_node(state: RuntimeHarness, root: Path, node_repo: Path) -> None:
    """
    新节点注册与账户亲和迁移是显式夹具，其后采用实际会话、领取和内容授权服务。

    :param state (RuntimeHarness): 恢复数据库中的原身份
    :param root (Path): 已恢复内容卷
    :param node_repo (Path): 包含独立 Linux 物化验收的节点仓库
    """
    replacement = replace(state, node=uuid4())
    async with state.database.begin() as session:
        old = await session.get(Node, state.node)
        assert old is not None
        old.status = "offline"
        session.add(
            Node(id=replacement.node, name="恢复验收新节点", status="healthy", region_code="global")
        )
        await session.flush()
    await ready(replacement)
    settings = Settings(
        secret_key="test", skill_manager_enabled=True, skill_storage_root=root / "objects"
    )
    async with state.database() as session:
        original = await session.get(Session, state.session)
        user = await session.get(User, state.owner)
        assert original is not None and user is not None
        workspace = await session.get(Workspace, original.workspace_id)
        assert workspace is not None
        created = await ToolSessionService(session, settings).create_session(
            user=user,
            tool_type="claude",
            tool_account_id=state.account,
            workspace_id=workspace.id,
            project_key=workspace.project_key,
            argv=[],
        )
    async with state.database() as session:
        node = await session.get(Node, replacement.node)
        assert node is not None
        snapshot = await session.scalar(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.session_id == created.id)
        )
        assert snapshot is not None and snapshot.node_id == replacement.node
        tasks = await NodeService(session, settings).poll_tasks(node=node, limit=100)
        assert snapshot.prepare_task_id in {task.id for task in tasks}
        snapshot_id, task_id = snapshot.id, snapshot.prepare_task_id
    exported = root.parent / "downloaded"
    exported.mkdir(mode=0o700)
    objects = exported / "objects"
    objects.mkdir(mode=0o700)
    store = PrivateObjectStore(root / "objects")
    async with state.database.begin() as session:
        content = NodeSkillContentService(session, store, SkillStoragePolicy())
        with pytest.raises(SkillContentError) as denied:
            await content.describe(state.node, snapshot_id, task_id)
        assert denied.value.code == "SNAPSHOT_NOT_FOUND"
        view = await content.describe(replacement.node, snapshot_id, task_id)
    manifest = exported / "manifest.json"
    manifest.write_text(view.manifest.model_dump_json())
    manifest.chmod(0o600)
    for entry in view.manifest.entries:
        if entry.kind != "file":
            continue
        async with state.database.begin() as session:
            content = NodeSkillContentService(session, store, SkillStoragePolicy())
            download = await content.authorize_file(
                replacement.node, snapshot_id, task_id, entry.sha256
            )
        destination = objects / entry.sha256
        with destination.open("wb") as target:
            await content.copy_authorized_file(download, target)
        destination.chmod(0o600)
        async with state.database.begin() as session:
            await NodeSkillContentService(session, store, SkillStoragePolicy()).authorize_file(
                replacement.node, snapshot_id, task_id, entry.sha256
            )
    output = run_fixture_command(
        ["bash", str(node_repo / "tests/linux_skill_restore_test.sh"), str(exported)]
    )
    assert b"--- PASS: TestRestoredServerSnapshotMaterializesOnFreshNode" in output
