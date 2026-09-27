"""
显式启用真实跨仓库首次使用验收，默认测试不启动 Docker 或网络监听。
"""

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_first_use_live_support import run_first_use_worker, serve_first_use
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
    NodeTaskResult,
    Session,
    ToolAccount,
)
from agent_remote_server.models.skill_deployment_discovery import SkillDeploymentDiscovery
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_preparation import SkillEffectiveBranch
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_library import SkillAddRequest
from agent_remote_server.security.tokens import hash_token

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_FIRST_USE_TEST") != "1",
    reason="requires explicit disposable Docker/systemd cross-repository acceptance",
)


@pytest.mark.parametrize("drop", [False, True], ids=["normal", "lost-takeover-confirmation"])
async def test_ordinary_first_use_worker_systemd_and_helper(
    takeover: TakeoverHarness, tmp_path: Path, drop: bool
) -> None:
    """
    普通受理产生首次接管，真实子写入者排空后完成原始发现与部署，不预设执行权威。

    :param takeover (TakeoverHarness): 没有目录状态或任务的账户
    :param tmp_path (Path): 独立数据库、内容与临时凭据
    :param drop (bool): 是否验证接管提交后的确认丢失
    """
    repo = Path(
        os.environ.get(
            "AGENT_REMOTE_TEST_NODE_REPO",
            str(Path(__file__).resolve().parents[2] / "agent-remote-node"),
        )
    ).resolve()
    assert (repo / "tests/linux_skill_first_use_test.sh").is_file(), (
        "missing Node acceptance harness"
    )
    secret, user_token, node_token = (secrets.token_urlsafe(32) for _ in range(3))
    takeover.settings = takeover.settings.model_copy(
        update={
            "secret_key": secret,
            "log_level": "CRITICAL",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/unused-app.db",
        }
    )
    async with takeover.library.database.begin() as session:
        account = await session.get(ToolAccount, takeover.account)
        node = await session.get(Node, takeover.node)
        assert account is not None and node is not None
        account.status = "active"
        node.node_token_hash = hash_token(secret, node_token)
        node.supported_tool_types = [account.tool_type]
        node.runtime_capabilities = {
            "backends": ["native"],
            "skill_manager": {"native": capability() | {"deployment_protocol_version": 1}},
        }
        session.add(
            AuthToken(
                user_id=takeover.library.owner,
                token_type="user",
                status="active",
                token_hash=hash_token(secret, user_token),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    request = SkillAddRequest(
        idempotency_key=str(uuid4()),
        expected_generation=0,
        items=(await takeover.library.candidate(name="installed"),),
    )
    async with (
        serve_first_use(takeover, drop) as server,
        AsyncClient(
            base_url=server.local_url, headers={"Authorization": "Bearer " + user_token}, timeout=30
        ) as client,
    ):
        accepted = await client.post(
            "/api/v1/skills/installations", json=request.model_dump(mode="json")
        )
        assert accepted.status_code == 200, accepted.text
        original = accepted.json()
        assert original["status"] == "preparing" and original["committed"]
        target = original["data"]["targets"][0]
        assert target["readiness"] == "pending"
        async with takeover.library.database() as session:
            for model in (
                NodeTask,
                NodeTaskResult,
                SkillAccountTakeover,
                SkillDeploymentTask,
                AccountSkillDirectoryState,
                Session,
                SessionSkillSnapshot,
                SkillEffectiveBranch,
            ):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
        await run_first_use_worker(
            repo,
            tmp_path / "first-use-fixture.json",
            {
                "url": server.worker_url,
                "token": node_token,
                "node_id": str(takeover.node),
                "operation_id": original["operation_id"],
                "drop_takeover_confirmation": drop,
            },
        )
        assert server.dropped == int(drop)
        status = await client.get(f"/api/v1/skills/operations/{original['operation_id']}")
        assert status.status_code == 200 and status.json()["status"] == "ready", status.text
        replay = await client.post(
            "/api/v1/skills/installations", json=request.model_dump(mode="json")
        )
        assert replay.json() == status.json()
    async with takeover.library.database() as session:
        boundary = await session.scalar(select(SkillDeploymentDiscovery))
        receipt = await session.scalar(select(SkillAccountTakeover))
        directory = await session.scalar(select(AccountSkillDirectoryState))
        assert boundary is not None and receipt is not None and directory is not None
        assert boundary.operation_id == UUID(original["operation_id"])
        assert boundary.original_digest == target["plan_digest"]
        assert boundary.resolved_digest != boundary.original_digest
        assert boundary.takeover_id == receipt.id and receipt.status == "committed"
        assert directory.mode == "managed_v1"
        assert await session.scalar(select(func.count()).select_from(NodeTask)) == 2
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 2
        for model in (Session, SessionSkillSnapshot, SkillEffectiveBranch):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
