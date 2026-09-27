"""
验证目录接管期间不能通过绑定或后端迁移重新派发旧共享目录写入者。
"""

from copy import deepcopy

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import select
from test_skill_config_import import counts
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_session_admission import ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuthToken, ToolAccount, ToolAccountProfile, User
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.services.skills.legacy_writers import require_legacy_account_writer
from agent_remote_server.services.tool_accounts import ToolAccountService


@pytest.mark.parametrize("mode", ["migrating", "managed_v1"])
@pytest.mark.parametrize("operation", ["binding", "backend"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_nonlegacy_writer_planning_is_atomic_even_when_feature_disabled(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    mode: str,
    operation: str,
    enabled: bool,
) -> None:
    """
    调用方捕获错误并提交仍不改变账户、配置档案、派发任务或审计。

    :param user_client (AsyncClient): 创建真实所有者令牌的客户端
    :param stopped (RuntimeHarness): 已有目录状态的账户
    :param mode (str): 接管中或已接管模式
    :param operation (str): 绑定或后端迁移入口
    :param enabled (bool): 新受管会话功能开关
    """
    await ready(stopped)
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        directory.mode = mode
    before = await counts(stopped)
    async with stopped.database.begin() as session:
        account = await session.get(ToolAccount, stopped.account)
        actor = await session.get(User, stopped.owner)
        credential = await session.scalar(
            select(AuthToken).where(AuthToken.user_id == stopped.owner)
        )
        profile = await session.scalar(
            select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == stopped.account)
        )
        assert account is not None and actor is not None and credential is not None
        original = account.status, account.runtime_backend, account.affinity_node_id
        original_profile = deepcopy(profile.profile_json) if profile is not None else None
        service = ToolAccountService(session, Settings(skill_manager_enabled=enabled))
        with pytest.raises(ApiError) as error:
            if operation == "binding":
                await service.start_binding(user=actor, token=credential, account_id=account.id)
            else:
                await service.migrate_runtime(
                    actor=actor, account_id=account.id, target_backend="docker_sandbox"
                )
        assert error.value.code == "MIGRATION_PENDING"
        assert (account.status, account.runtime_backend, account.affinity_node_id) == original
        assert (profile.profile_json if profile is not None else None) == original_profile
    assert await counts(stopped) == before


@pytest.mark.parametrize("operation", ["binding", "backend"])
async def test_http_legacy_writer_rejection_preserves_account(
    user_client: AsyncClient, stopped: RuntimeHarness, operation: str
) -> None:
    """
    真实用户及管理员入口均返回稳定错误而不接受等待中的旧写入者任务。

    :param user_client (AsyncClient): 真实用户令牌客户端
    :param stopped (RuntimeHarness): 已受管目录
    :param operation (str): 绑定或管理员后端迁移接口
    """
    await ready(stopped)
    async with stopped.database.begin() as session:
        actor = await session.get(User, stopped.owner)
        assert actor is not None
        actor.role = "admin"
    before = await counts(stopped)
    path = f"/api/v1/tool-accounts/{stopped.account}"
    if operation == "binding":
        response = await user_client.post(path + "/bind/start")
    else:
        response = await user_client.post(
            path + "/runtime-migration", json={"target_runtime_backend": "docker_sandbox"}
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MIGRATION_PENDING"
    assert await counts(stopped) == before


async def test_legacy_writer_guard_refreshes_cached_directory_mode(stopped: RuntimeHarness) -> None:
    """
    用户锁后的查询刷新 ORM 旧模式，不能借之前读到的 legacy 绕过接管。

    :param stopped (RuntimeHarness): 独立数据库中的账户
    """
    async with stopped.database.begin() as session:
        directory = await session.get(AccountSkillDirectoryState, stopped.account)
        assert directory is not None
        directory.mode = "legacy"
    async with stopped.database() as session:
        cached = await session.get(AccountSkillDirectoryState, stopped.account)
        assert cached is not None and cached.mode == "legacy"
        await session.commit()
        async with stopped.database.begin() as other:
            directory = await other.get(AccountSkillDirectoryState, stopped.account)
            assert directory is not None
            directory.mode = "migrating"
        with pytest.raises(ApiError) as error:
            await require_legacy_account_writer(session, stopped.owner, stopped.account)
        assert error.value.code == "MIGRATION_PENDING"
        assert cached.mode == "migrating"
