"""
验证账户启动迁移保留真实学习状态，最后一步失败也不留下部分准备或使用账本。
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select, update
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate
from test_skill_migration import request as migration_request
from test_skill_migration_conflicts import pending
from test_skill_preparation import request as preparation_request
from test_skill_preparation import update_version
from test_skill_session_admission import launch, ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, Node, NodeTask, Session, ToolAccount, User
from agent_remote_server.models.skill_preparation import (
    SkillBranchPreparation,
    SkillEffectiveBranch,
)
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillCheckpoint,
)
from agent_remote_server.schemas.skill_library import SkillUpdateRequest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.services.sessions import ToolSessionService
from agent_remote_server.services.skills.account_preparation import _key
from agent_remote_server.services.skills.session_admission import (
    SkillAdmissionPending,
    SkillSessionAdmission,
)
from agent_remote_server.services.skills.snapshots import SkillSnapshotService
from agent_remote_server.services.skills.takeover_admission import SkillTakeoverPending


async def test_account_preparation_migrates_learning_and_includes_other_enabled_skills(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    首次进入新版自动保留旧学习，其他独立启用来源也出现在同一完整快照中。

    :param stopped (RuntimeHarness): 使用旧版本的账户
    :param tmp_path (Path): 私有卷
    """
    await publish(
        stopped,
        tmp_path,
        await ingest(stopped, tmp_path, {"learning/memory": b"keep learned data"}),
    )
    revision = await update_version(stopped, tmp_path, "new upstream")
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="notes"))
    await ready(stopped)
    created = await launch(stopped, tmp_path)
    async with stopped.database() as session:
        snapshot = await session.scalar(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.session_id == created.id)
        )
        assert snapshot is not None
        tree = await content_service(session, tmp_path).read_tree(
            stopped.owner, "state", snapshot.tree_digest
        )
        assert {"learning/memory", "notes/SKILL.md"}.issubset(
            {entry.path for entry in tree.entries}
        )
        preparations = (
            await session.scalars(
                select(SkillBranchPreparation).where(
                    SkillBranchPreparation.user_id == stopped.owner
                )
            )
        ).all()
        assert {row.mode for row in preparations} == {"forward", "initial"}
        assert all(row.status == "ready" for row in preparations)
        effective = (
            await session.scalars(
                select(SkillEffectiveBranch).where(SkillEffectiveBranch.user_id == stopped.owner)
            )
        ).all()
        assert len(effective) == 2 and all(row.snapshot_id == snapshot.id for row in effective)
        branches = (
            await session.scalars(
                select(AccountSkillState).where(
                    AccountSkillState.id.in_([row.state_id for row in effective])
                )
            )
        ).all()
        assert revision in {row.base_revision_id for row in branches}


async def persisted_counts(state: RuntimeHarness) -> tuple[int | None, ...]:
    """
    检查启动涉及的全部可变引用，不依赖服务返回值判断保存点是否回滚。

    :param state (RuntimeHarness): 用户数据库
    :return tuple[int | None, ...]: 每类对象的用户内计数
    """
    async with state.database() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (
                    Session,
                    NodeTask,
                    SkillBranchPreparation,
                    SkillCheckpoint,
                    SessionSkillSnapshot,
                    SkillEffectiveBranch,
                    AuditLog,
                )
            ]
        )


@pytest.mark.parametrize("step", ["snapshot", "audit"])
async def test_late_admission_failure_rolls_back_even_when_outer_transaction_commits(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """
    快照或审计最后一步失败后，外层捕获并提交也不能保留本次部分启动与学习迁移。

    :param prepared (RuntimeHarness): 未初始化的账户
    :param tmp_path (Path): 私有卷
    :param monkeypatch (pytest.MonkeyPatch): 晚到失败注入
    :param step (str): 快照或审计边界
    """
    await ready(prepared)
    before = await persisted_counts(prepared)
    reserve = SkillSnapshotService.reserve
    audit = ToolSessionService._audit

    async def fail_snapshot(
        self: SkillSnapshotService,
        user_id: UUID,
        session_id: UUID,
        task_id: UUID,
        system_releases: dict[str, object],
    ) -> SessionSkillSnapshot:
        """
        全部快照引用已保存之后失败。

        :param user_id (UUID): 所有者
        :param session_id (UUID): 新会话
        :param task_id (UUID): 准备任务
        :param system_releases (dict[str, object]): 固定系统引用
        :return SessionSkillSnapshot: 故障注入不会返回
        """
        await reserve(self, user_id, session_id, task_id, system_releases)
        raise RuntimeError("late admission failure")

    async def fail_audit(
        self: ToolSessionService,
        *,
        actor_user_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, object],
    ) -> None:
        """
        会话与任务审计均写入后失败。

        :param actor_user_id (UUID | None): 当前用户
        :param action (str): 审计动作
        :param target_type (str): 对象类型
        :param target_id (str): 对象身份
        :param details (dict[str, object]): 无内容的审计信息
        """
        await audit(
            self,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        raise RuntimeError("late admission failure")

    with monkeypatch.context() as patch:
        if step == "snapshot":
            patch.setattr(SkillSnapshotService, "reserve", fail_snapshot)
        else:
            patch.setattr(ToolSessionService, "_audit", fail_audit)
        async with prepared.database.begin() as session:
            user = await session.get(User, prepared.owner)
            original = await session.get(Session, prepared.session)
            assert user is not None and original is not None
            service = ToolSessionService(
                session,
                Settings(
                    secret_key="test",
                    skill_manager_enabled=True,
                    skill_storage_root=tmp_path / "objects",
                ),
            )
            with pytest.raises(RuntimeError, match="late admission failure"):
                await service.create_session(
                    user=user,
                    tool_type="claude",
                    tool_account_id=prepared.account,
                    workspace_id=original.workspace_id,
                    project_key=original.project_key,
                    argv=[],
                )
    assert await persisted_counts(prepared) == before
    async with prepared.database() as session:
        branch = await session.get(AccountSkillState, prepared.state)
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert branch is not None and branch.head_checkpoint_id is None
        assert directory is not None and directory.head_checkpoint_id == prepared.directory
    assert (await launch(prepared, tmp_path)).status == "starting"


async def test_all_account_conflicts_are_retained_and_retry_reuses_empty_target_identities(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    多个首次目标都冲突时返回全部身份，首次创建空分支不能让重试产生重复计划。

    :param stopped (RuntimeHarness): 旧版本账户
    :param tmp_path (Path): 私有卷
    """
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.add(await library.candidate(name="notes"))
    active = await new_session(stopped, tmp_path)
    await publish(
        active,
        tmp_path,
        await ingest(
            active,
            tmp_path,
            {"learning/SKILL.md": b"local learning", "notes/SKILL.md": b"local notes"},
        ),
    )
    await update_version(stopped, tmp_path, "new learning")
    await library.execute(
        SkillUpdateRequest(
            skill="notes",
            item=await library.candidate(name="notes", version="new notes"),
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    await ready(stopped)
    with pytest.raises(ApiError) as first:
        await launch(stopped, tmp_path)
    assert first.value.code == "STATE_MIGRATION_REQUIRED"
    identities = first.value.details["migration_ids"]
    assert isinstance(identities, list) and len(identities) == 2
    before = await persisted_counts(stopped)
    with pytest.raises(ApiError) as second:
        await launch(stopped, tmp_path)
    assert first.value.details == second.value.details
    assert await persisted_counts(stopped) == before


async def test_public_key_collision_cannot_substitute_an_incremental_attempt_for_preparation(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    内部键前缀不是授权，用户预先占用同键的另一类操作不能充当当前账户准备。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    original = await pending(stopped, tmp_path)
    assert isinstance(original, SkillMigrationView)
    selected = await preparation_request(stopped, tmp_path)
    key = _key(stopped.account, selected.expected)
    explicit = await migration_request(
        stopped, tmp_path, original.before.source.revision_id, original.before.target.revision_id
    )
    await migrate(stopped, tmp_path, explicit.model_copy(update={"idempotency_key": key}))
    await ready(stopped)
    before = await persisted_counts(stopped)
    with pytest.raises(ApiError) as error:
        await launch(stopped, tmp_path)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert await persisted_counts(stopped) == before


async def test_capability_revocation_after_preparation_refreshes_node_and_rolls_back(
    prepared: RuntimeHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    准备后数据库报告变化不能被旧 ORM 缓存掩盖，外层提交也不能留下部分启动。

    :param prepared (RuntimeHarness): 未初始化的账户
    :param tmp_path (Path): 私有内容卷
    :param monkeypatch (pytest.MonkeyPatch): 准备后的报告撤回注入
    """
    await ready(prepared)
    before = await persisted_counts(prepared)
    prepare = SkillSessionAdmission.prepare

    async def revoke(
        self: SkillSessionAdmission, account: ToolAccount, node: Node
    ) -> SkillAdmissionPending | SkillTakeoverPending | None:
        """
        使用不刷新身份缓存的数据库写入模拟准备期间撤回报告。

        :param account (ToolAccount): 启动账户
        :param node (Node): 本次兼容节点
        :return SkillAdmissionPending | SkillTakeoverPending | None: 原准备结果
        """
        result = await prepare(self, account, node)
        await self.preparation.service._session.execute(
            update(Node)
            .where(Node.id == prepared.node)
            .values(runtime_capabilities={"backends": ["native"]})
            .execution_options(synchronize_session=False)
        )
        return result

    monkeypatch.setattr(SkillSessionAdmission, "prepare", revoke)
    async with prepared.database.begin() as session:
        user = await session.get(User, prepared.owner)
        original = await session.get(Session, prepared.session)
        assert user is not None and original is not None
        service = ToolSessionService(
            session,
            Settings(
                secret_key="test",
                skill_manager_enabled=True,
                skill_storage_root=tmp_path / "objects",
            ),
        )
        with pytest.raises(ApiError) as error:
            await service.create_session(
                user=user,
                tool_type="claude",
                tool_account_id=prepared.account,
                workspace_id=original.workspace_id,
                project_key=original.project_key,
                argv=[],
            )
        assert error.value.code == "SKILL_MANAGER_UNSUPPORTED"
    assert await persisted_counts(prepared) == before
