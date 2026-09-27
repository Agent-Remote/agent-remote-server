"""
验证受管启动只接受完整当前后端能力，空库与旧能力均不能触发降级。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified
from test_skill_content_service import database as database
from test_skill_session_admission import capability, launch, ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, Session
from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
)
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_takeover import SkillAccountTakeover


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "other_backend",
        "protocol",
        "bool_protocol",
        "manifest",
        "writable",
        "finalization",
        "recovery",
        "coerced_bool",
        "stale",
        "no_heartbeat",
        "no_backend",
    ],
)
async def test_incomplete_or_stale_backend_report_cannot_create_any_preparation(
    prepared: RuntimeHarness, tmp_path: Path, change: str
) -> None:
    """
    版本不能强制转换，其他后端能力与旧心跳都不能授权准备，更不能产生启动对象。

    :param prepared (RuntimeHarness): 已接管账户
    :param tmp_path (Path): 内容卷
    :param change (str): 缺失或失效能力类别
    """
    await ready(prepared)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        report = capability()
        reports = {"native": report}
        if change == "missing":
            reports = {}
        elif change == "other_backend":
            reports = {"docker_sandbox": report}
        elif change in {
            "protocol",
            "bool_protocol",
            "manifest",
            "writable",
            "finalization",
            "recovery",
            "coerced_bool",
        }:
            field, value = {
                "protocol": ("protocol_version", 2),
                "bool_protocol": ("protocol_version", True),
                "manifest": ("manifest_version", "1"),
                "writable": ("writable_copies", False),
                "finalization": ("finalization", False),
                "recovery": ("recovery", False),
                "coerced_bool": ("recovery", 1),
            }[change]
            report[field] = value
        elif change == "stale":
            node.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
        elif change == "no_heartbeat":
            node.last_heartbeat_at = None
        node.runtime_capabilities = {
            "skill_manager": reports,
            **({} if change == "no_backend" else {"backends": ["native"]}),
        }
        flag_modified(node, "runtime_capabilities")
    with pytest.raises(ApiError) as error:
        await launch(prepared, tmp_path)
    assert error.value.code == "SKILL_MANAGER_UNSUPPORTED"
    async with prepared.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillBranchPreparation)
                .where(SkillBranchPreparation.user_id == prepared.owner)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(Session).where(Session.user_id == prepared.owner)
            )
            == 1
        )


async def test_empty_managed_account_still_receives_a_snapshot(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    全部停用只改变快照成员，不改变账户权威或省略最终状态收尾身份。

    :param prepared (RuntimeHarness): 已接管账户
    :param tmp_path (Path): 内容卷
    """
    await ready(prepared)
    async with prepared.database.begin() as session:
        item = await session.scalar(
            select(SkillInstallation).where(SkillInstallation.user_id == prepared.owner)
        )
        assert item is not None
        item.default_enabled = False
    created = await launch(prepared, tmp_path)
    async with prepared.database() as session:
        snapshot = await session.scalar(
            select(SessionSkillSnapshot).where(SessionSkillSnapshot.session_id == created.id)
        )
        assert snapshot is not None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SessionSkillSnapshotItem)
                .where(SessionSkillSnapshotItem.snapshot_id == snapshot.id)
            )
            == 0
        )
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert (
            directory is not None
            and directory.mode == "managed_v1"
            and directory.head_checkpoint_id == prepared.directory
        )


@pytest.mark.parametrize(
    ("mode", "expected_error"),
    [("legacy", "HEAD_CHANGED"), ("migrating", "TAKEOVER_RECOVERY_REQUIRED")],
)
async def test_capable_node_cannot_repair_inconsistent_account_takeover(
    prepared: RuntimeHarness, tmp_path: Path, mode: str, expected_error: str
) -> None:
    """
    能力齐备不能修复保留已有 head 的原始目录或缺少预约的迁移目录。

    :param prepared (RuntimeHarness): 已有目录检查点的账户
    :param tmp_path (Path): 内容卷
    :param mode (str): 与已保存权威不一致的目录模式
    :param expected_error (str): 明确拒绝而非普通预约等待的错误
    """
    await ready(prepared)
    async with prepared.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory.mode = mode
    with pytest.raises(ApiError) as error:
        await launch(prepared, tmp_path)
    assert error.value.code == expected_error
    assert not error.value.details or not error.value.details.get("reservation_committed")
    async with prepared.database() as session:
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        assert directory.mode == mode and directory.head_checkpoint_id == prepared.directory
        assert (
            await session.scalar(
                select(func.count()).select_from(Session).where(Session.user_id == prepared.owner)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(NodeTask).where(NodeTask.node_id == prepared.node)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SessionSkillSnapshot)
                .where(SessionSkillSnapshot.user_id == prepared.owner)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillAccountTakeover)
                .where(SkillAccountTakeover.user_id == prepared.owner)
            )
            == 0
        )
