"""
从真实旧账户构建接管事务测试，跨请求重建服务以验证持久化边界。
"""

import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_library import LibraryHarness
from test_skill_session_admission import capability
from test_skill_storage import file_entry

from agent_remote_server.config import Settings
from agent_remote_server.models import Node, NodeTask, ToolAccount
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_takeover import SkillTakeoverCapture, SkillTakeoverRequest
from agent_remote_server.services.skills.takeover import SkillAccountTakeoverService


@dataclass
class TakeoverHarness:
    """
    保留身份而不缓存事务服务或上传权威。
    """

    library: LibraryHarness
    node: UUID
    account: UUID
    settings: Settings

    def service(self, session: AsyncSession) -> SkillAccountTakeoverService:
        """
        为每次请求创建事务服务。

        :param session (AsyncSession): 当前事务
        :return SkillAccountTakeoverService: 接管服务
        """
        return SkillAccountTakeoverService(session, self.settings)

    async def reserve(self, key: str = "takeover", epoch: int = 0) -> SkillAccountTakeover:
        """
        提交一次旧目录预约。

        :param key (str): 原始请求键
        :param epoch (int): 比较纪元
        :return SkillAccountTakeover: 已持久化收据
        """
        async with self.library.database.begin() as session:
            return await self.service(session).reserve(
                self.library.owner,
                self.account,
                SkillTakeoverRequest(idempotency_key=key, expected_directory_epoch=epoch),
            )

    async def lease(self, receipt: SkillAccountTakeover) -> None:
        """
        模拟节点正常领取确切任务。

        :param receipt (SkillAccountTakeover): 原始预约
        """
        async with self.library.database.begin() as session:
            task = await session.get(NodeTask, receipt.task_id)
            assert task is not None
            task.status = "running"
            task.lease_until = datetime.now(UTC) + timedelta(minutes=5)

    def capture(
        self, receipt: SkillAccountTakeover, manifest: SkillTreeManifest
    ) -> SkillTakeoverCapture:
        """
        构造同一固定输入的节点声明。

        :param receipt (SkillAccountTakeover): 原始预约
        :param manifest (SkillTreeManifest): 完整冻结目录
        :return SkillTakeoverCapture: 捕获声明
        """
        return SkillTakeoverCapture(
            helper_receipt_id=uuid4(),
            directory_epoch=receipt.directory_epoch,
            inventory_digest=receipt.inventory_digest,
            writers_quiescent=True,
            manifest=manifest,
        )

    async def begin(
        self, receipt: SkillAccountTakeover, request: SkillTakeoverCapture
    ) -> SkillAccountTakeover:
        """
        通过新事务绑定一次上传尝试。

        :param receipt (SkillAccountTakeover): 原始预约
        :param request (SkillTakeoverCapture): 固定捕获输入
        :return SkillAccountTakeover: 当前绑定
        """
        async with self.library.database.begin() as session:
            return await self.service(session).begin_capture(
                self.node, receipt.id, receipt.task_id, request
            )

    async def transfer(self, receipt: SkillAccountTakeover, files: dict[str, bytes]) -> None:
        """
        按摘要逐个传输真实字节，每次重新验证任务权限。

        :param receipt (SkillAccountTakeover): 绑定上传的收据
        :param files (dict[str, bytes]): 相对路径与内容
        """
        assert receipt.upload_id is not None
        for path, data in files.items():
            entry = file_entry(data, path=path)
            async with self.library.database.begin() as session:
                service = self.service(session)
                assert await service.prepare_file(
                    self.node, receipt.id, receipt.task_id, receipt.upload_id, entry.sha256
                )
                await service.put_file(
                    self.node,
                    receipt.id,
                    receipt.task_id,
                    receipt.upload_id,
                    entry.sha256,
                    io.BytesIO(data),
                )

    async def complete(self, receipt: SkillAccountTakeover) -> SkillAccountTakeover:
        """
        提交目录权威并返回持久化收据。

        :param receipt (SkillAccountTakeover): 已完成文件传输的绑定
        :return SkillAccountTakeover: 最初权威提交结果
        """
        assert receipt.upload_id is not None
        async with self.library.database.begin() as session:
            return await self.service(session).complete(
                self.node, receipt.id, receipt.task_id, receipt.upload_id
            )


@pytest.fixture
async def takeover(library: LibraryHarness) -> TakeoverHarness:
    """
    创建无任何受管目录或分支的固定后端账户。

    :param library (LibraryHarness): 新用户与私有内容卷
    :return TakeoverHarness: 未接管账户入口
    """
    account_id, node_id = await library.account(), uuid4()
    async with library.database.begin() as session:
        session.add(
            Node(
                id=node_id,
                name="接管测试",
                status="healthy",
                region_code="global",
                allowed_runtime_backends=["native", "docker_sandbox"],
                runtime_capabilities={
                    "backends": ["native", "docker_sandbox"],
                    "skill_manager": {"native": capability(), "docker_sandbox": capability()},
                },
                last_heartbeat_at=datetime.now(UTC),
            )
        )
        await session.flush()
        account = await session.get(ToolAccount, account_id)
        assert account is not None
        account.affinity_node_id, account.runtime_backend = node_id, "native"
    return TakeoverHarness(
        library,
        node_id,
        account_id,
        Settings(
            secret_key="test",
            skill_manager_enabled=True,
            skill_storage_root=library.root / "objects",
        ),
    )


def tree(files: dict[str, bytes], extras: tuple[SkillTreeEntry, ...] = ()) -> SkillTreeManifest:
    """
    为测试内容补齐显式父目录并保留空目录和跨入口链接。

    :param files (dict[str, bytes]): 文件与内容
    :param extras (tuple[SkillTreeEntry, ...]): 额外目录或链接
    :return SkillTreeManifest: 完整规范清单
    """
    entries = {entry.path: entry for entry in extras}
    for path, content in files.items():
        entries[path] = file_entry(content, path=path)
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            entries.setdefault(parent, SkillTreeEntry(path=parent, kind="directory", mode=0o755))
    return SkillTreeManifest(entries=tuple(entries[key] for key in sorted(entries)))


async def writer_task(
    state: TakeoverHarness,
    kind: str = "binding",
    status: str = "cancelled",
    node_id: UUID | None = None,
) -> NodeTask:
    """
    写入历史任务，刻意不依赖账户 profile 的最新绑定身份。

    :param state (TakeoverHarness): 测试账户
    :param kind (str): 旧写入者种类
    :param status (str): 当前任务状态
    :param node_id (UUID | None): 可选历史节点
    :return NodeTask: 已提交任务
    """
    task_type = {
        "binding": "create_binding_session",
        "session": "create_tool_session",
        "import": "import_tool_account_config",
        "backend": "migrate_tool_account_runtime",
    }[kind]
    task = NodeTask(
        id=uuid4(),
        node_id=node_id or state.node,
        task_id=str(uuid4()),
        task_type=task_type,
        status=status,
        payload={
            "user_id": str(state.library.owner),
            "tool_account_id": str(state.account),
            "binding_id": str(uuid4()),
            "session_id": str(uuid4()),
            "files": [{"path": "~/.claude/skills/manual/SKILL.md"}],
        },
    )
    async with state.library.database.begin() as session:
        session.add(task)
    return task


async def legacy_session(state: TakeoverHarness, status: str = "running") -> UUID:
    """
    创建没有历史派发任务但仍须检查的旧会话。

    :param state (TakeoverHarness): 测试账户
    :param status (str): 控制面状态
    :return UUID: 持久化会话身份
    """
    from agent_remote_server.models import Session, UserDevice, Workspace

    async with state.library.database.begin() as session:
        device = UserDevice(
            user_id=state.library.owner, name="测试", platform="linux", status="active"
        )
        session.add(device)
        await session.flush()
        workspace = Workspace(
            user_id=state.library.owner,
            device_id=device.id,
            project_key="takeover",
            local_start_path="/tmp/test",
            display_name="测试",
        )
        session.add(workspace)
        await session.flush()
        legacy = Session(
            id=uuid4(),
            user_id=state.library.owner,
            tool_account_id=state.account,
            node_id=state.node,
            workspace_id=workspace.id,
            project_key="takeover",
            tool_type="claude",
            runtime_backend="native",
            status=status,
        )
        session.add(legacy)
    return legacy.id
