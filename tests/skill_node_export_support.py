"""
构造真实认证的冻结导出测试，不通过依赖替换伪造用户或节点身份。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_session
from agent_remote_server.config import Settings
from agent_remote_server.main import create_app
from agent_remote_server.models import AuthToken, Node, SshKey, UserDevice
from agent_remote_server.schemas.skill_node_export import NodeExportRequest
from agent_remote_server.security.tokens import hash_token


@dataclass
class ExportHarness:
    """
    只保存测试身份和仅用于本次内存请求的凭据。
    """

    state: RuntimeHarness
    settings: Settings
    token_id: UUID
    request: NodeExportRequest
    user_token: str = field(repr=False)
    node_token: str = field(repr=False)


@pytest.fixture
async def export_state(stopped: RuntimeHarness, tmp_path: Path) -> ExportHarness:
    """
    在尚无终止报告或上传记录的原始快照上登记真实设备与用户凭据。

    :param stopped (RuntimeHarness): 原始精确启动快照
    :param tmp_path (Path): 私有测试内容卷
    :return ExportHarness: 可独立重建请求的真实认证身份
    """
    device_id, key_id, token_id = uuid4(), uuid4(), uuid4()
    user_token, node_token = str(uuid4()), str(uuid4())
    settings = Settings(
        secret_key="node-export-test",
        skill_manager_enabled=True,
        log_level="CRITICAL",
        skill_storage_root=tmp_path / "objects",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    async with stopped.database.begin() as session:
        node = await session.get(Node, stopped.node)
        assert node is not None
        node.status, node.ssh_host = "healthy", "127.0.0.1"
        node.node_token_hash = hash_token(settings.secret_key, node_token)
        node.runtime_capabilities = {}
        session.add(
            UserDevice(
                id=device_id,
                user_id=stopped.owner,
                name="导出测试",
                platform="linux",
                status="active",
            )
        )
        await session.flush()
        session.add(
            SshKey(
                id=key_id,
                user_device_id=device_id,
                public_key="ssh-ed25519 disposable-export-fixture",
                fingerprint=str(key_id),
                status="active",
            )
        )
        session.add(
            AuthToken(
                id=token_id,
                user_id=stopped.owner,
                token_type="user",
                status="active",
                token_hash=hash_token(settings.secret_key, user_token),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    return ExportHarness(
        stopped,
        settings,
        token_id,
        NodeExportRequest(device_id=device_id, ssh_key_id=key_id),
        user_token,
        node_token,
    )


@pytest.fixture
async def export_client(export_state: ExportHarness) -> AsyncIterator[AsyncClient]:
    """
    只替换数据库连接，保留全部实际认证与路由。

    :param export_state (ExportHarness): 私有测试身份
    :return AsyncIterator[AsyncClient]: 真实用户认证客户端
    """
    app = create_app(export_state.settings)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        每次请求使用独立可回滚事务。

        :return AsyncIterator[AsyncSession]: 当前数据库会话
        """
        async with export_state.state.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + export_state.user_token},
        ) as client:
            yield client
    finally:
        await app.state.database_engine.dispose()
