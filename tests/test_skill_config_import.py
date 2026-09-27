"""
验证旧配置导入在规划、旧任务启动和节点写入授权处遵守当前技能目录所有权。
"""

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_session_admission import ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuditLog, NodeTask, ToolAccountProfile, User
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.schemas.tool_accounts import ToolAccountConfigImportFile
from agent_remote_server.services.tool_accounts import ToolAccountService


def request(paths: list[str], dry_run: bool = False) -> dict[str, object]:
    """
    生成包含多种配置的真实导入请求。

    :param paths (list[str]): 完整文件路径
    :param dry_run (bool): 是否只做规划
    :return dict[str, object]: 用户 API 请求
    """
    return {
        "tool_type": "claude",
        "source": "local_cli",
        "include": paths,
        "exclude": [],
        "files": [
            {"path": path, "content_base64": base64.b64encode(b"config").decode(), "mode": 384}
            for path in paths
        ],
        "include_resume_history": False,
        "dry_run": dry_run,
    }


async def counts(state: RuntimeHarness) -> tuple[int | None, ...]:
    """
    失败前后检查任务、配置档案及审计均未发生部分写入。

    :param state (RuntimeHarness): 独立所有者上下文
    :return tuple[int | None, ...]: 当前数据库引用计数
    """
    async with state.database() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (NodeTask, ToolAccountProfile, AuditLog)
            ]
        )


@pytest.mark.parametrize("mode", ["managed_v1", "migrating"])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("path", ["~/.claude/skills", "$HOME/.claude/skills/demo/SKILL.md"])
async def test_import_planning_rejects_entire_mixed_batch(
    user_client: AsyncClient, stopped: RuntimeHarness, mode: str, dry_run: bool, path: str
) -> None:
    """
    任一技能根或子路径都拒绝整批，预览和正式请求具有相同所有权边界。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 已有目录状态
    :param mode (str): 当前权威模式
    :param dry_run (bool): 预览或正式导入
    :param path (str): 不同拼写的技能路径
    """
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        directory.mode = mode
    before = await counts(stopped)
    response = await user_client.post(
        f"/api/v1/tool-accounts/{stopped.account}/config-imports",
        json=request(["~/.claude/settings.json", path], dry_run),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SKILL_MANAGER_OWNS_PATH"
    assert await counts(stopped) == before


async def test_files_cannot_hide_owned_path_outside_include_or_behind_exclusion(
    user_client: AsyncClient, stopped: RuntimeHarness
) -> None:
    """
    请求文件列表不能借缺失 include 或自报 exclude 绕过整批拒绝。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 受管账户
    """
    body = request(["~/.claude/settings.json", "~/.claude/skills/demo/SKILL.md"])
    body["include"] = ["~/.claude/settings.json"]
    body["exclude"] = ["~/.claude/skills"]
    response = await user_client.post(
        f"/api/v1/tool-accounts/{stopped.account}/config-imports", json=body
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SKILL_MANAGER_OWNS_PATH"


async def queued(
    client: AsyncClient, state: RuntimeHarness, paths: list[str]
) -> tuple[str, dict[str, str]]:
    """
    创建真实任务并授予精确未来租约，保留真实节点认证。

    :param client (AsyncClient): 用户客户端
    :param state (RuntimeHarness): 目标账户
    :param paths (list[str]): 文件路径
    :return tuple[str, dict[str, str]]: 任务身份与节点认证头
    """
    await ready(state)
    response = await client.post(
        f"/api/v1/tool-accounts/{state.account}/config-imports", json=request(paths)
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["data"]["task_id"]
    async with state.database.begin() as session:
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        assert task is not None
        task.status = "leased"
        task.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    value = await token(state, state.owner, "node")
    return task_id, {"Authorization": f"Bearer {value}"}


async def test_non_skill_import_remains_available_and_authorization_contains_no_content(
    user_client: AsyncClient, stopped: RuntimeHarness
) -> None:
    """
    受管账户仍可迁移普通配置及插件内容，响应只包含精确身份与模式。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 受管账户
    """
    task_id, headers = await queued(
        user_client, stopped, ["~/.claude/settings.json", "~/.claude/plugins/demo/skills/SKILL.md"]
    )
    path = f"/api/v1/node-api/tasks/{task_id}"
    assert (await user_client.post(path + "/start", headers=headers)).status_code == 200
    response = await user_client.get(path + "/config-import-authorization", headers=headers)
    assert response.status_code == 200
    assert response.json()["data"] == {
        "task_id": task_id,
        "node_id": str(stopped.node),
        "user_id": str(stopped.owner),
        "account_id": str(stopped.account),
        "directory_mode": "managed_v1",
        "directory_epoch": 1,
    }
    assert "content_base64" not in response.text and "config" not in response.json()["data"]
    assert (await user_client.get(path + "/config-import-authorization")).status_code == 401


@pytest.mark.parametrize("mode", ["migrating", "managed_v1"])
async def test_queued_legacy_import_is_rejected_by_start_and_fresh_authorization(
    user_client: AsyncClient, stopped: RuntimeHarness, mode: str
) -> None:
    """
    迁移前合法排队的整批任务必须按执行时模式重新判断，不允许先写普通配置。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 保有初始目录的账户
    :param mode (str): 排队后切换的目录模式
    """
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        directory.mode = "legacy"
    task_id, headers = await queued(
        user_client, stopped, ["~/.claude/settings.json", "~/.claude/skills/demo/SKILL.md"]
    )
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        directory.mode = mode
    before = await counts(stopped)
    path = f"/api/v1/node-api/tasks/{task_id}"
    for response in (
        await user_client.post(path + "/start", headers=headers),
        await user_client.get(path + "/config-import-authorization", headers=headers),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SKILL_MANAGER_OWNS_PATH"
    assert await counts(stopped) == before
    async with stopped.database() as session:
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        assert task is not None and task.status == "leased"


@pytest.mark.parametrize("invalid", ["pending", "expired", "terminal", "owner", "account", "kind"])
async def test_execution_authorization_requires_exact_live_task(
    user_client: AsyncClient, stopped: RuntimeHarness, invalid: str
) -> None:
    """
    无租约、失效归属、错误任务类型都不能取得写入前授权。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 受管账户
    :param invalid (str): 失效边界
    """
    task_id, headers = await queued(user_client, stopped, ["~/.claude/settings.json"])
    async with stopped.database.begin() as session:
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        assert task is not None
        if invalid == "pending":
            task.status = "pending"
        elif invalid == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif invalid == "terminal":
            task.status = "succeeded"
        elif invalid == "owner":
            user = await session.get(User, stopped.owner)
            assert user is not None
            user.status = "disabled"
        elif invalid == "account":
            task.payload = {**task.payload, "tool_account_id": str(stopped.owner)}
        else:
            task.task_type = "create_tool_session"
    response = await user_client.get(
        f"/api/v1/node-api/tasks/{task_id}/config-import-authorization", headers=headers
    )
    assert response.status_code == 404


async def test_disabled_feature_does_not_release_managed_ownership(
    stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    关闭功能开关也不能重新允许旧工具写受管目录，捕获错误后提交仍无部分写入。

    :param stopped (RuntimeHarness): 受管账户
    :param tmp_path (Path): 私有卷
    """
    before = await counts(stopped)
    async with stopped.database.begin() as session:
        user = await session.get(User, stopped.owner)
        assert user is not None
        service = ToolAccountService(
            session,
            Settings(secret_key="test", skill_manager_enabled=False, skill_storage_root=tmp_path),
        )
        with pytest.raises(ApiError) as error:
            await service.plan_config_import(
                user=user,
                account_id=stopped.account,
                tool_type="claude",
                include=["~/.claude/skills"],
                exclude=[],
                files=[
                    ToolAccountConfigImportFile(
                        path="~/.claude/skills/demo/SKILL.md", content_base64="YQ==", mode=384
                    )
                ],
                include_resume_history=False,
                dry_run=False,
            )
        assert error.value.code == "SKILL_MANAGER_OWNS_PATH"
    assert await counts(stopped) == before


async def test_import_wire_quota_counts_metadata_and_html_escaping_before_dispatch(
    user_client: AsyncClient, stopped: RuntimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    原始内容很小也不能靠巨大路径绕过传输上限，超限请求不派发任务。

    :param user_client (AsyncClient): 真实用户客户端
    :param stopped (RuntimeHarness): 目标账户
    :param monkeypatch (pytest.MonkeyPatch): 小型部署传输限额
    """
    await ready(stopped)
    monkeypatch.setattr(
        "agent_remote_server.services.tool_accounts.CONFIG_IMPORT_MAX_ENCODED_BYTES", 300
    )
    before = await counts(stopped)
    response = await user_client.post(
        f"/api/v1/tool-accounts/{stopped.account}/config-imports",
        json=request(["~/.claude/rules/" + "<&>" * 15 + ".md"]),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONFIG_IMPORT_TOO_LARGE"
    assert await counts(stopped) == before
