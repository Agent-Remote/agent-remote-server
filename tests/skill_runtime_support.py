"""
为运行态约束和服务测试创建有真实归属关系的完整快照。
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import service, user
from test_skill_library import LibraryHarness

from agent_remote_server.models import Node, NodeTask, Session, UserDevice, Workspace
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


@dataclass
class RuntimeHarness:
    """
    保存已提交的归属标识，避免测试通过关闭外键绕过约束。
    """

    database: async_sessionmaker[AsyncSession]
    owner: UUID
    account: UUID
    node: UUID
    task: UUID
    session: UUID
    state: UUID
    directory: UUID
    item: UUID
    snapshot: UUID
    tree: str


async def runtime(database: async_sessionmaker[AsyncSession], root: Path) -> RuntimeHarness:
    """
    创建用户库分支、真实会话及完整目录快照。

    :param database (async_sessionmaker[AsyncSession]): 数据库工厂
    :param root (Path): 私有内容目录
    :return RuntimeHarness: 已提交的测试身份
    """
    root.mkdir(parents=True, exist_ok=True)
    owner = await user(database)
    library = LibraryHarness(database, root, owner)
    await library.add(await library.candidate())
    info = await library.info()
    account = await library.account()
    node, task, session_id, state_id, directory_id, item_id, snapshot_id = (
        uuid4() for _ in range(7)
    )
    async with database.begin() as session:
        content = service(session, root)
        upload = await content.begin(owner, str(uuid4()), SkillTreeManifest(entries=()), "state")
        tree = await content.complete(owner, upload.id)
        session.add(
            AccountSkillDirectoryState(user_id=owner, account_id=account, tool_type="claude")
        )
        session.add(Node(id=node, name="运行态测试", status="healthy", region_code="global"))
        device = UserDevice(user_id=owner, name="测试设备", platform="linux", status="active")
        session.add(device)
        await session.flush()
        workspace = Workspace(
            user_id=owner,
            device_id=device.id,
            project_key="runtime",
            local_start_path="/tmp/test",
            display_name="测试工作区",
        )
        session.add(workspace)
        session.add(
            NodeTask(
                id=task,
                node_id=node,
                task_id=str(task),
                task_type="prepare_session",
                status="pending",
            )
        )
        session.add(
            AccountSkillState(
                id=state_id,
                user_id=owner,
                account_id=account,
                installation_id=info.id,
                installation_epoch=info.epoch,
                base_revision_id=info.default_revision_id,
            )
        )
        await session.flush()
        session.add(
            Session(
                id=session_id,
                tool_type="claude",
                user_id=owner,
                tool_account_id=account,
                workspace_id=workspace.id,
                node_id=node,
                project_key="runtime",
                status="starting",
                runtime_backend="native",
            )
        )
        session.add_all(
            [
                SkillCheckpoint(
                    id=directory_id,
                    user_id=owner,
                    account_id=account,
                    scope="directory",
                    content_digest=tree.digest,
                    tree_digest=tree.digest,
                ),
                SkillCheckpoint(
                    id=item_id,
                    user_id=owner,
                    account_id=account,
                    scope="item",
                    state_id=state_id,
                    subtree_prefix="learning",
                    content_digest=tree.digest,
                    tree_digest=tree.digest,
                ),
            ]
        )
        await session.flush()
        session.add(
            SkillDirectoryMember(
                user_id=owner,
                account_id=account,
                directory_checkpoint_id=directory_id,
                state_id=state_id,
                checkpoint_id=item_id,
                entry_name="learning",
            )
        )
        session.add(
            SessionSkillSnapshot(
                id=snapshot_id,
                user_id=owner,
                account_id=account,
                node_id=node,
                session_id=session_id,
                session_reference_id=session_id,
                prepare_task_id=task,
                runtime_backend="native",
                library_generation=1,
                directory_epoch=1,
                starting_checkpoint_id=directory_id,
                tree_digest=tree.digest,
                system_releases_json={},
            )
        )
    return RuntimeHarness(
        database,
        owner,
        account,
        node,
        task,
        session_id,
        state_id,
        directory_id,
        item_id,
        snapshot_id,
        tree.digest,
    )
