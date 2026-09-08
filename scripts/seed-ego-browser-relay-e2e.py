#!/usr/bin/env python3
"""为跨仓库 relay E2E 创建一次性控制面 fixture。"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_remote_server.config import Settings
from agent_remote_server.db import Base, create_engine, create_session_factory
from agent_remote_server.models import (
    AuthToken,
    Node,
    Session,
    ToolAccount,
    User,
    UserDevice,
    Workspace,
)
from agent_remote_server.security import create_opaque_token, hash_token

SKILL_TREE_SHA256 = "262110a09678fd3e0bbb382400588dacb98b24659b3b4a57903703b65d133c7c"


async def seed() -> dict[str, str]:
    """
    创建公开 API 所需的最小真实身份和会话

    :return dict[str, str]: fixture 中各实体标识和临时令牌
    """

    settings = Settings()
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(settings, engine)
        async with session_factory() as session:
            now = datetime.now(UTC)
            user_token = create_opaque_token("art")
            node_token = create_opaque_token("node")
            user = User(
                username="ego-relay-e2e",
                display_name="Ego Relay E2E",
                role="user",
                status="active",
                password_hash="integration-only",
                totp_enabled=False,
            )
            node = Node(
                name="ego-relay-e2e-node",
                status="healthy",
                region_code="ZZ",
                tags=["integration"],
                weight=1,
                supported_tool_types=["claude"],
                allowed_runtime_backends=["native"],
                default_runtime_backend="native",
                runtime_policy={},
                runtime_capabilities={
                    "ego_browser_bridge": {
                        "supported": True,
                        "protocol_versions": ["ego-browser-bridge-v1"],
                        "wrapper_version": "0.1.0",
                        "skill_version": "1.2.3",
                        "skill_tree_sha256": SKILL_TREE_SHA256,
                        "remote_platform": "linux",
                        "local_platform": "macos",
                        "max_script_bytes": 1_048_576,
                        "max_execute_timeout_ms": 120_000,
                    }
                },
                node_token_hash=hash_token(settings.secret_key, node_token),
                last_heartbeat_at=now,
                version="0.2.14",
            )
            session.add_all([user, node])
            await session.flush()
            user_device = UserDevice(
                user_id=user.id,
                name="fixture-only-workspace-device",
                platform="macos",
                status="active",
                last_seen_at=now,
            )
            account = ToolAccount(
                user_id=user.id,
                tool_type="claude",
                display_name="Fixture Claude",
                status="active",
                region_code="ZZ",
                timezone="UTC",
                locale="en_US.UTF-8",
                preferred_node_tags=[],
                affinity_node_id=node.id,
                runtime_backend="native",
            )
            session.add_all([user_device, account])
            await session.flush()
            workspace = Workspace(
                user_id=user.id,
                device_id=user_device.id,
                project_key="sha256:ego-browser-relay-e2e",
                local_start_path="/tmp/ego-browser-relay-e2e",
                display_name="Ego Browser Relay E2E",
                remote_path="/tmp/ego-browser-relay-e2e",
                sync_git=False,
                git_sync_policy={},
            )
            session.add(workspace)
            await session.flush()
            tool_session = Session(
                tool_type="claude",
                user_id=user.id,
                tool_account_id=account.id,
                workspace_id=workspace.id,
                node_id=node.id,
                project_key=workspace.project_key,
                status="running",
                tmux_session_name="ego-relay-e2e",
                runtime_backend="native",
                runtime_resource_id="ego-relay-e2e",
            )
            auth_token = AuthToken(
                user_id=user.id,
                user_device_id=None,
                token_hash=hash_token(settings.secret_key, user_token),
                token_type="user",
                status="active",
                expires_at=now + timedelta(hours=1),
            )
            session.add_all([tool_session, auth_token])
            await session.commit()
            return {
                "node_id": str(node.id),
                "node_token": node_token,
                "tool_session_id": str(tool_session.id),
                "user_token": user_token,
            }
    finally:
        await engine.dispose()


def main() -> None:
    """
    把 fixture 写为仅所有者可读的 JSON 文档。

    :raises ValueError: fixture 输出路径不是绝对路径
    """

    output = Path(os.environ["EGO_BROWSER_E2E_FIXTURE_PATH"])
    if not output.is_absolute():
        raise ValueError("EGO_BROWSER_E2E_FIXTURE_PATH must be absolute")
    output.write_text(json.dumps(asyncio.run(seed()), sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)


if __name__ == "__main__":
    main()
