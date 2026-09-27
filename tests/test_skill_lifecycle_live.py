"""
通过独立非 root Worker、正式 Helper 和真实 Claude 验证 Native 学习继承。
"""

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_first_use_live_support import serve_first_use
from skill_lifecycle_live_support import LifecycleReports, lifecycle_app, run_lifecycle_worker
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_library import library as library
from test_skill_session_admission import capability

from agent_remote_server.models import (
    AuthToken,
    Node,
    NodeTask,
    Session,
    ToolAccount,
    UserDevice,
    Workspace,
)
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.schemas.skill_library import SkillAddRequest
from agent_remote_server.security.tokens import hash_token

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_LIFECYCLE_TEST") != "1"
    and os.environ.get("AGENT_REMOTE_RUN_SKILL_DAEMON_TEST") != "1",
    reason="requires explicit real Claude credentials and disposable Docker/systemd acceptance",
)


async def prepare_fixture(state: TakeoverHarness, root: Path) -> dict[str, str]:
    """
    只创建原有身份、账户与已同步工作区，不预设任何技能运行权威。

    :param state (TakeoverHarness): 未接管账户
    :param root (Path): 本次隔离配置根目录
    :return dict[str, str]: 一次性守护进程和用户连接信息
    """
    secret, user_token, node_token = (secrets.token_urlsafe(32) for _ in range(3))
    workspace_id = uuid4()
    state.settings = state.settings.model_copy(
        update={
            "secret_key": secret,
            "log_level": "CRITICAL",
            "database_url": f"sqlite+aiosqlite:///{root}/unused-app.db",
        }
    )
    async with state.library.database.begin() as session:
        node = await session.get(Node, state.node)
        account = await session.get(ToolAccount, state.account)
        assert node is not None and account is not None
        account.status = "active"
        account.locale = "en_US.UTF-8"
        node.node_token_hash = hash_token(secret, node_token)
        node.supported_tool_types = ["claude"]
        node.allowed_runtime_backends = ["native"]
        node.default_runtime_backend = "native"
        node.runtime_capabilities = {
            "backends": ["native"],
            "skill_manager": {"native": capability() | {"deployment_protocol_version": 1}},
        }
        device = UserDevice(
            user_id=state.library.owner, name="隔离验收", platform="linux", status="active"
        )
        session.add(device)
        await session.flush()
        session.add(
            Workspace(
                id=workspace_id,
                user_id=state.library.owner,
                device_id=device.id,
                project_key="skill-lifecycle",
                local_start_path="/disposable-project",
                display_name="隔离生命周期",
                remote_path=f"/var/lib/agent-remote/users/{state.library.owner}/workspaces/{workspace_id}/files",
                sync_git=False,
            )
        )
        session.add(
            AuthToken(
                user_id=state.library.owner,
                token_type="user",
                status="active",
                token_hash=hash_token(secret, user_token),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    return {
        "token": node_token,
        "user_token": user_token,
        "node_id": str(state.node),
        "user_id": str(state.library.owner),
        "account_id": str(state.account),
        "workspace_id": str(workspace_id),
    }


@pytest.mark.parametrize("learning", [False, True], ids=["runtime", "learning"])
async def test_real_native_claude_lifecycle(
    takeover: TakeoverHarness, tmp_path: Path, learning: bool
) -> None:
    """
    从普通安装到两次真实学习、收尾、回收和进程重启，要求独立原始快照。

    :param takeover (TakeoverHarness): 未接管的独立账户
    :param tmp_path (Path): 私有临时凭据与数据
    :param learning (bool): 是否验证两次实际推理和学习继承
    """
    if learning and os.environ.get("AGENT_REMOTE_RUN_SKILL_LIFECYCLE_TEST") != "1":
        pytest.skip("real inference requires separate explicit opt-in and credentials")
    if learning:
        assert os.environ.get("AGENT_REMOTE_TEST_CLAUDE_CREDENTIALS")
    for key in (
        "AGENT_REMOTE_TEST_CLAUDE_BINARY",
        "AGENT_REMOTE_TEST_CLAUDE_SHA256",
    ):
        assert os.environ.get(key), f"explicit lifecycle acceptance requires {key}"
    repo = Path(
        os.environ.get(
            "AGENT_REMOTE_TEST_NODE_REPO",
            str(Path(__file__).resolve().parents[2] / "agent-remote-node"),
        )
    ).resolve()
    values = await prepare_fixture(takeover, tmp_path)
    candidate = await takeover.library.candidate(
        name="learning",
        content=(
            b"---\nname: learning\ndescription: Store learned facts for subsequent sessions.\n"
            b"---\nUse this skill to remember and recall facts across sessions.\n"
            b"To remember a fact, write its exact bytes to memory.txt in this skill directory, "
            b"with no newline. To recall it, read memory.txt here and write its exact bytes "
            b"to /workspace/inherited.txt, with no newline. Do not change other files.\n"
        ),
    )
    reports = LifecycleReports()
    app = lifecycle_app(takeover, reports)
    async with (
        serve_first_use(takeover, False, app) as server,
        AsyncClient(
            base_url=server.local_url,
            headers={"Authorization": "Bearer " + values["user_token"]},
            timeout=30,
        ) as client,
    ):
        accepted = await client.post(
            "/api/v1/skills/installations",
            json=SkillAddRequest(
                idempotency_key=str(uuid4()), expected_generation=0, items=(candidate,)
            ).model_dump(mode="json"),
        )
        assert accepted.status_code == 200
        operation = accepted.json()
        assert operation["committed"] and operation["status"] == "preparing"
        async with takeover.library.database() as session:
            for model in (NodeTask, Session, SessionSkillSnapshot, AccountSkillDirectoryState):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
        try:
            await run_lifecycle_worker(
                repo,
                tmp_path / "lifecycle-fixture.json",
                values | {"url": server.worker_url, "operation_id": operation["operation_id"]},
                learning=learning,
            )
        except AssertionError as error:
            raise AssertionError(f"{error}\nHTTP stages: {reports.responses}") from None
    assert reports.heartbeats >= 1 and reports.reconciliations >= 1
    async with takeover.library.database() as session:
        sessions = list(await session.scalars(select(Session)))
        snapshots = list(await session.scalars(select(SessionSkillSnapshot)))
        finalizations = list(await session.scalars(select(SkillFinalization)))
        publications = list(await session.scalars(select(SkillPublication)))
        expected = 2 if learning else 1
        assert (
            len(sessions) == len(snapshots) == len(finalizations) == len(publications) == expected
        )
        assert all(item.status == "stopped" for item in sessions)
        assert all(item.status == "retained" for item in snapshots)
        assert all(item.status == "published" and not item.unclean for item in finalizations)
        assert all(item.status == "published" for item in publications)
        assert {item.snapshot_id for item in finalizations} == {item.id for item in snapshots}
