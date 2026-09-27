"""
用真实 CLI、认证 HTTP、非特权 Worker 和正式 Helper 验证被动迁移恢复。
"""

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from live_acceptance_support import build_cli, prepare_cli_user
from runtime_recovery_live_support import (
    RecoveryReports,
    recovery_app,
    recovery_cli,
    run_recovery_node,
)
from skill_first_use_live_support import serve_first_use
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models import (
    AuthToken,
    Node,
    NodeTask,
    NodeTaskResult,
    ToolAccount,
    ToolAccountProfile,
    User,
)
from agent_remote_server.security.tokens import hash_token

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_RUNTIME_RECOVERY_TEST") != "1",
    reason="requires explicit systemd, CLI and cross-repository HTTP acceptance",
)


async def recovery_fixture(state: TakeoverHarness, root: Path) -> dict[str, str | bool]:
    """
    仅建立隔离活动身份和旧账户，原迁移及恢复只能由生产接口生成。

    :param state (TakeoverHarness): 无受管状态的账户
    :param root (Path): 隔离内容根
    :return dict[str, str | bool]: 一次性身份与凭据
    """
    __tracebackhide__ = True
    secret, token, user_token = (secrets.token_urlsafe(32) for _ in range(3))
    state.settings = state.settings.model_copy(
        update={
            "secret_key": secret,
            "log_level": "CRITICAL",
            "database_url": f"sqlite+aiosqlite:///{root}/unused-app.db",
            "skill_manager_enabled": False,
            "node_task_lease_seconds": 10,
        }
    )
    async with state.library.database.begin() as session:
        user = await session.get(User, state.library.owner)
        node = await session.get(Node, state.node)
        account = await session.get(ToolAccount, state.account)
        assert user is not None and node is not None and account is not None
        user.role = "admin"
        node.node_token_hash = hash_token(secret, token)
        node.runtime_capabilities = {"backends": ["docker_sandbox", "native"]}
        node.supported_tool_types = ["claude"]
        account.status, account.runtime_backend = "active", "docker_sandbox"
        session.add(
            AuthToken(
                user_id=user.id,
                token_type="user",
                status="active",
                token_hash=hash_token(secret, user_token),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    return {
        "token": token,
        "user_token": user_token,
        "node_id": str(state.node),
        "user_id": str(state.library.owner),
        "account_id": str(state.account),
        "recovery_key": str(uuid4()),
    }


async def original_failure(state: TakeoverHarness, task_id: str) -> tuple[UUID, dict[str, object]]:
    """
    等待真实 Worker 结果提交，保留原失败用于恢复后的不可变比较。

    :param state (TakeoverHarness): 隔离数据库
    :param task_id (str): 实际 API 创建的迁移任务
    :return tuple[UUID, dict[str, object]]: 原结果身份和无内容错误
    """
    async with asyncio.timeout(40):
        while True:
            async with state.library.database() as session:
                task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
                result = await session.scalar(
                    select(NodeTaskResult).where(NodeTaskResult.task_id == task_id)
                )
                if task is not None and result is not None:
                    assert task.status == result.status == "failed"
                    assert result.error is not None and result.result is None
                    return result.id, result.error
            await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    "action", [None, "verify_source", "repair_source"], ids=["target", "source", "repair"]
)
@pytest.mark.parametrize("missing_backup", [False, True], ids=["lost-replies", "missing-backup"])
async def test_runtime_recovery_cli_and_production_daemons(
    takeover: TakeoverHarness, tmp_path: Path, missing_backup: bool, action: str | None
) -> None:
    """
    验证显式恢复在真实进程中断和回执丢失后仍保留原始权限证据。

    :param takeover (TakeoverHarness): 独立原账户与内容
    :param tmp_path (Path): 私有命令配置和一次性凭据
    :param missing_backup (bool): 是否验证缺失备份继续阻塞而不重新复制
    :param action (str | None): 目标完成、源验证或中断源修复动作
    """
    version = {None: 1, "verify_source": 2, "repair_source": 3}[action]
    git_root = Path(__file__).resolve().parents[2]
    node_repo = Path(
        os.environ.get("AGENT_REMOTE_TEST_NODE_REPO", str(git_root / "agent-remote-node"))
    ).resolve()
    cli_repo = Path(
        os.environ.get("AGENT_REMOTE_TEST_CLI_REPO", str(git_root / "agent-remote-cli"))
    ).resolve()
    binary = await build_cli(cli_repo)
    values = await recovery_fixture(takeover, tmp_path)
    user_token = str(values["user_token"])
    reports = RecoveryReports(lose_replies=not missing_backup)
    app = recovery_app(takeover, reports)
    account_path = f"/api/v1/tool-accounts/{takeover.account}"
    async with (
        serve_first_use(takeover, False, app) as server,
        AsyncClient(
            base_url=server.local_url, headers={"Authorization": "Bearer " + user_token}, timeout=30
        ) as client,
    ):
        response = await client.post(
            account_path + "/runtime-migration", json={"target_runtime_backend": "native"}
        )
        assert response.status_code == 200
        original_task = str(response.json()["data"]["task_id"])
        key = str(values["recovery_key"])
        cli_root = tmp_path / "cli"
        prepare_cli_user(cli_root, server.local_url, user_token)
        values |= {
            "url": server.worker_url,
            "original_task_id": original_task,
            "missing_backup": missing_backup,
            "verify_source": action == "verify_source",
            "repair_source": action == "repair_source",
        }
        async with run_recovery_node(node_repo, tmp_path / "node-fixture.json", values) as node:
            await node.wait_ready()
            original_id, original_error = await original_failure(takeover, original_task)
            disabled = await client.patch(account_path, json={"status": "disabled"})
            assert disabled.status_code == 200
            args = [
                "recover-runtime",
                str(takeover.account),
                "--original-task",
                original_task,
                "--request-id",
                key,
            ]
            if action:
                args.append("--" + action.replace("_", "-"))
            code, accepted, diagnostic = await recovery_cli(binary, cli_root, args)
            if missing_backup:
                assert code == 0 and accepted is not None
            else:
                assert code == 1 and accepted is not None and diagnostic == ""
                assert accepted["schema_version"] == version
                assert accepted["account_id"] == str(takeover.account)
                assert accepted["request_id"] == key
                assert accepted["original_task_id"] == original_task
                assert accepted["error_code"] == "RECOVERY_ACCEPTANCE_UNKNOWN"
                assert accepted["acceptance"] == "unknown"
                assert accepted["recovery"] is None
                assert accepted["target_completion_confirmed"] is None
                assert accepted["next_command"] == (
                    f"agent-remote account recovery-status {takeover.account} --request-id {key}"
                )
                if action:
                    assert accepted["action"] == action
                    assert accepted["source_restoration_confirmed"] is None
                assert reports.acceptance_lost == 1
            code, status, _ = await recovery_cli(
                binary, cli_root, ["recovery-status", str(takeover.account), "--request-id", key]
            )
            assert code == 0 and status is not None
            binding = cast(
                dict[str, object], cast(dict[str, object], status["recovery"])["binding"]
            )
            assert binding["original_task_id"] == original_task
            await node.finish()
        code, final, _ = await recovery_cli(
            binary, cli_root, ["recovery-status", str(takeover.account), "--request-id", key]
        )
        assert code == 0 and final is not None
        expected = "failed" if missing_backup else "succeeded"
        assert cast(dict[str, object], final["recovery"])["status"] == expected
        assert final["target_completion_confirmed"] == (not missing_backup and not action)
        if action:
            assert final["source_restoration_confirmed"] is not missing_backup
            assert binding["version"] == version and binding["action"] == action
        code, replay, _ = await recovery_cli(binary, cli_root, args)
        assert code == 0 and replay == final
        assert reports.completion_lost == int(not missing_backup)
        assert reports.completion_rejected == int(not missing_backup)
        async with takeover.library.database() as session:
            account = await session.get(ToolAccount, takeover.account)
            profile = await session.scalar(
                select(ToolAccountProfile).where(
                    ToolAccountProfile.tool_account_id == takeover.account
                )
            )
            assert account is not None and profile is not None
            assert account.status == "disabled"
            assert account.runtime_backend == (
                "docker_sandbox" if missing_backup or action else "native"
            )
            original = await session.get(NodeTaskResult, original_id)
            assert (
                original is not None
                and original.status == "failed"
                and original.error == original_error
            )
            assert await session.scalar(select(func.count()).select_from(NodeTask)) == 2
            assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 2
            migration = cast(dict[str, object], profile.profile_json["runtime_migration"])
            assert migration["status"] == (
                "recovery_required" if missing_backup else "rolled_back" if action else "succeeded"
            )
            task = await session.get(NodeTask, UUID(str(binding["task_record_id"])))
            assert task is not None and task.status == expected
            if not missing_backup:
                assert task.retry_count >= 2
                result = await session.scalar(
                    select(NodeTaskResult).where(NodeTaskResult.node_task_id == task.id)
                )
                assert result is not None and result.result is not None
                authority = cast(dict[str, object], result.result["authorization"])
                assert authority["lease_attempt"] == task.retry_count
            node_record = await session.get(Node, takeover.node)
            assert (
                node_record is not None
                and node_record.runtime_capabilities.get("skill_manager", {}) == {}
            )
    assert reports.routes.get(
        "GET /node-api/tasks/{task_id}/runtime-migration-recovery-authorization 200", 0
    ) >= (1 if missing_backup else 2)
