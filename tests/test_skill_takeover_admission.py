"""
验证普通启动只能预约原节点接管，提交前不创建新会话或强停旧写入者。
"""

import asyncio
import hashlib
from uuid import UUID, uuid4

import pytest
from skill_takeover_support import takeover as takeover
from skill_takeover_support import tree
from sqlalchemy import select
from takeover_admission_support import TakeoverAdmission
from takeover_admission_support import admission as admission
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, Session, ToolAccount, Workspace
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
)
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_takeover import SkillAccountTakeover


async def pending(admission: TakeoverAdmission) -> UUID:
    """
    普通启动返回可重试的已提交预约，并明确没有创建新会话。

    :param admission (TakeoverAdmission): 原始启动输入
    :return UUID: 持久化接管身份
    """
    with pytest.raises(ApiError) as error:
        await admission.launch()
    assert error.value.code == "MIGRATION_PENDING"
    details = error.value.details
    assert details["reservation_committed"] is True and details["session_created"] is False
    assert details["account_id"] == str(admission.state.account)
    return UUID(str(details["takeover_id"]))


async def test_session_takeover_reserves_once_without_stopping_old_session(
    admission: TakeoverAdmission,
) -> None:
    """
    并发重试只共用一个预约，原进程继续运行且没有新启动任务或快照。

    :param admission (TakeoverAdmission): 仍有旧会话的账户
    """
    first, second = await pending(admission), await pending(admission)
    assert first == second == await pending(admission)
    state = admission.state
    async with state.library.database() as session:
        receipt = await session.get(SkillAccountTakeover, first)
        directory = await session.get(AccountSkillDirectoryState, state.account)
        original = await session.get(Session, admission.original)
        assert receipt is not None and receipt.status == "reserved"
        assert directory is not None and directory.mode == "migrating"
        assert original is not None and original.status == "running"
        tasks = (
            await session.scalars(select(NodeTask).where(NodeTask.node_id == state.node))
        ).all()
        assert len(tasks) == 1 and tasks[0].task_type == "takeover_tool_account_skills"
        assert tasks[0].id == receipt.task_id and receipt.node_id == state.node
        assert not list(
            await session.scalars(
                select(SessionSkillSnapshot).where(
                    SessionSkillSnapshot.user_id == state.library.owner
                )
            )
        )
        sessions = (
            await session.scalars(select(Session).where(Session.user_id == state.library.owner))
        ).all()
        assert len(sessions) == 1


async def test_session_takeover_preserves_manual_content_then_admits_snapshot(
    admission: TakeoverAdmission,
) -> None:
    """
    节点保存原手工树后重试普通启动，同时包含手工状态和用户库技能。

    :param admission (TakeoverAdmission): 原始启动输入
    """
    identity = await pending(admission)
    state = admission.state
    async with state.library.database.begin() as session:
        receipt = await session.get(SkillAccountTakeover, identity)
        original = await session.get(Session, admission.original)
        assert receipt is not None and original is not None
        original.status = "stopped"
    await state.lease(receipt)
    files = {
        "manual/SKILL.md": b"---\nname: manual\ndescription: Manual\n---\n",
        "manual/state.bin": b"\x00learning",
    }
    uploading = await state.begin(receipt, state.capture(receipt, tree(files)))
    await state.transfer(uploading, files)
    committed = await state.complete(uploading)
    created = await admission.launch()
    async with state.library.database() as session:
        snapshot = await session.scalar(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.session_id == created.id)
        )
        assert snapshot is not None and snapshot.node_id == state.node
        assert committed.checkpoint_id is not None
        assert created.id != admission.original and created.status == "starting"
        task = await session.get(NodeTask, snapshot.prepare_task_id)
        assert task is not None
        pointer = task.payload["skill_manager"]
        assert isinstance(pointer, dict) and pointer["snapshot_id"] == str(snapshot.id)
        names = await session.scalars(
            select(SessionSkillSnapshotItem.entry_name).where(
                SessionSkillSnapshotItem.snapshot_id == snapshot.id
            )
        )
        assert set(names) == {"learning", "manual"}
        materialized = await state.service(session).context.content.read_tree(
            state.library.owner, "state", snapshot.tree_digest
        )
        learned = next(entry for entry in materialized.entries if entry.path == "manual/state.bin")
        assert learned.sha256 == hashlib.sha256(files["manual/state.bin"]).hexdigest()


@pytest.mark.parametrize("change", ["disabled", "backend", "node", "workspace", "account"])
async def test_takeover_admission_rejects_unavailable_or_unauthorized_original(
    admission: TakeoverAdmission, change: str
) -> None:
    """
    功能、原后端、原节点和既有请求授权失败不能留下新预约。

    :param admission (TakeoverAdmission): 原始启动输入
    :param change (str): 被撤销的准入条件
    """
    state = admission.state
    async with state.library.database.begin() as session:
        account = await session.get(ToolAccount, state.account)
        node = await session.get(Node, state.node)
        workspace = await session.get(Workspace, admission.workspace)
        assert account is not None and node is not None and workspace is not None
        if change == "disabled":
            state.settings.skill_manager_enabled = False
        elif change == "backend":
            account.runtime_backend = "docker_sandbox"
        elif change == "node":
            account.affinity_node_id = None
        elif change == "workspace":
            workspace.remote_path = None
        else:
            account.status = "disabled"
    with pytest.raises(ApiError):
        await admission.launch()
    async with state.library.database() as session:
        assert not list(
            await session.scalars(
                select(SkillAccountTakeover).where(
                    SkillAccountTakeover.user_id == state.library.owner
                )
            )
        )
        assert await session.get(AccountSkillDirectoryState, state.account) is None


@pytest.mark.parametrize("change", ["task", "payload", "node"])
async def test_takeover_admission_does_not_repair_changed_original_evidence(
    admission: TakeoverAdmission, change: str
) -> None:
    """
    已预约任务的终态、正文或账户绑定变化不能用新预约掩盖。

    :param admission (TakeoverAdmission): 原始启动输入
    :param change (str): 改变的原始证据
    """
    identity = await pending(admission)
    state = admission.state
    async with state.library.database.begin() as session:
        receipt = await session.get(SkillAccountTakeover, identity)
        assert receipt is not None
        task = await session.get(NodeTask, receipt.task_id)
        account = await session.get(ToolAccount, state.account)
        assert task is not None and account is not None
        if change == "task":
            task.status = "cancelled"
        elif change == "payload":
            task.payload = {**task.payload, "takeover_id": str(uuid4())}
        else:
            account.affinity_node_id = None
    with pytest.raises(ApiError) as error:
        await admission.launch()
    assert error.value.code != "MIGRATION_PENDING"
    async with state.library.database() as session:
        rows = list(
            await session.scalars(
                select(SkillAccountTakeover).where(
                    SkillAccountTakeover.user_id == state.library.owner
                )
            )
        )
        assert len(rows) == 1 and rows[0].id == identity


async def test_postgresql_concurrent_session_takeover_reserves_once(
    admission: TakeoverAdmission,
) -> None:
    """
    用 PostgreSQL 行锁验证两个普通启动事务共用唯一预约。

    :param admission (TakeoverAdmission): 已启用技能的旧账户
    """
    async with admission.state.library.database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL row-lock semantics")
    first, second = await asyncio.gather(pending(admission), pending(admission))
    assert first == second


async def test_takeover_reservation_rolls_back_when_capability_changes_before_return(
    admission: TakeoverAdmission, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    预约后能力撤回仍回滚整个启动保存点，不留下目录围栏或任务。

    :param admission (TakeoverAdmission): 原始启动输入
    :param monkeypatch (pytest.MonkeyPatch): 预约后撤销能力的注入器
    """
    from sqlalchemy import update

    from agent_remote_server.services.skills.session_admission import (
        SkillAdmissionPending,
        SkillSessionAdmission,
    )
    from agent_remote_server.services.skills.takeover_admission import SkillTakeoverPending

    prepare = SkillSessionAdmission.prepare

    async def revoke(
        self: SkillSessionAdmission, account: ToolAccount, node: Node
    ) -> SkillAdmissionPending | SkillTakeoverPending | None:
        """
        原预约之后改变数据库报告，不同步已加载的节点缓存。

        :param account (ToolAccount): 原账户
        :param node (Node): 原兼容节点
        :return SkillAdmissionPending | SkillTakeoverPending | None: 原准备结果
        """
        result = await prepare(self, account, node)
        await self.takeover.service.context.session.execute(
            update(Node)
            .where(Node.id == node.id)
            .values(runtime_capabilities={})
            .execution_options(synchronize_session=False)
        )
        return result

    monkeypatch.setattr(SkillSessionAdmission, "prepare", revoke)
    with pytest.raises(ApiError) as error:
        await admission.launch()
    assert error.value.code == "SKILL_MANAGER_UNSUPPORTED"
    async with admission.state.library.database() as session:
        assert await session.get(AccountSkillDirectoryState, admission.state.account) is None
        assert not list(
            await session.scalars(
                select(SkillAccountTakeover).where(
                    SkillAccountTakeover.user_id == admission.state.library.owner
                )
            )
        )


async def test_takeover_cannot_capture_empty_directory_on_replacement_node(
    admission: TakeoverAdmission,
) -> None:
    """
    普通调度可发现候选节点，但首次接管必须拒绝离线原节点的替代机器。

    :param admission (TakeoverAdmission): 原目录仍在原节点的账户
    """
    from datetime import UTC, datetime

    from test_skill_session_admission import capability

    state = admission.state
    async with state.library.database.begin() as session:
        original = await session.get(Session, admission.original)
        node = await session.get(Node, state.node)
        assert original is not None and node is not None
        original.status = "stopped"
        node.status = "offline"
        session.add(
            Node(
                id=uuid4(),
                name="备用节点",
                status="healthy",
                region_code=node.region_code,
                supported_tool_types=["claude"],
                allowed_runtime_backends=["native"],
                default_runtime_backend="native",
                runtime_capabilities={
                    "backends": ["native"],
                    "skill_manager": {"native": capability()},
                },
                last_heartbeat_at=datetime.now(UTC),
            )
        )
    with pytest.raises(ApiError) as error:
        await admission.launch()
    assert error.value.code == "SKILL_MANAGER_UNSUPPORTED"
    async with state.library.database() as session:
        assert await session.get(AccountSkillDirectoryState, state.account) is None
        account = await session.get(ToolAccount, state.account)
        assert account is not None and account.affinity_node_id == state.node


async def test_migrating_account_without_receipt_cannot_invent_recovery(
    admission: TakeoverAdmission,
) -> None:
    """
    遗留 migrating 状态缺少预约时保留现场，不能猜测清单后重新接管。

    :param admission (TakeoverAdmission): 原账户
    """
    state = admission.state
    async with state.library.database.begin() as session:
        session.add(
            AccountSkillDirectoryState(
                account_id=state.account,
                user_id=state.library.owner,
                tool_type="claude",
                epoch=7,
                mode="migrating",
            )
        )
    with pytest.raises(ApiError) as error:
        await admission.launch()
    assert error.value.code == "TAKEOVER_RECOVERY_REQUIRED"
    async with state.library.database() as session:
        directory = await session.get(AccountSkillDirectoryState, state.account)
        assert directory is not None and directory.epoch == 7
        assert not list(
            await session.scalars(
                select(SkillAccountTakeover).where(
                    SkillAccountTakeover.user_id == state.library.owner
                )
            )
        )
