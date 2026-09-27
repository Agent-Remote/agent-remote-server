"""
用真实旧账户、库条目和会话服务构建首次接管的启动边界。
"""

from dataclasses import dataclass
from uuid import UUID

import pytest
from skill_takeover_support import TakeoverHarness, legacy_session

from agent_remote_server.models import Node, Session, ToolAccount, User, Workspace
from agent_remote_server.services.sessions import ToolSessionService


@dataclass
class TakeoverAdmission:
    """
    保留启动输入，重试不依赖上次请求事务。
    """

    state: TakeoverHarness
    original: UUID
    workspace: UUID
    project: str

    def payload(self) -> dict[str, object]:
        """
        复用现有会话 API 字段，不允许客户端声明后端或快照。

        :return dict[str, object]: 原始启动输入
        """
        return {
            "tool_type": "claude",
            "tool_account_id": str(self.state.account),
            "workspace_id": str(self.workspace),
            "project_key": self.project,
            "argv": [],
        }

    async def launch(self) -> Session:
        """
        用新事务调用普通启动入口。

        :return Session: 受理成功的新会话
        """
        async with self.state.library.database() as session:
            owner = await session.get(User, self.state.library.owner)
            assert owner is not None
            return await ToolSessionService(session, self.state.settings).create_session(
                user=owner,
                tool_type="claude",
                tool_account_id=self.state.account,
                workspace_id=self.workspace,
                project_key=self.project,
                argv=[],
            )


@pytest.fixture
async def admission(takeover: TakeoverHarness) -> TakeoverAdmission:
    """
    创建仍在运行的旧会话以及已启用的用户库来源。

    :param takeover (TakeoverHarness): 旧账户与私有卷
    :return TakeoverAdmission: 可请求接管的完整普通启动输入
    """
    original_id = await legacy_session(takeover)
    async with takeover.library.database.begin() as session:
        account = await session.get(ToolAccount, takeover.account)
        node = await session.get(Node, takeover.node)
        original = await session.get(Session, original_id)
        assert account is not None and node is not None and original is not None
        workspace = await session.get(Workspace, original.workspace_id)
        assert workspace is not None
        workspace.remote_path = "/var/lib/agent-remote/workspaces/takeover"
        account.status = "active"
        account.region_code = node.region_code = takeover.library.owner.hex
        node.supported_tool_types = ["claude"]
        result = TakeoverAdmission(takeover, original_id, workspace.id, workspace.project_key)
    await takeover.library.add(await takeover.library.candidate())
    return result


async def admission_token(admission: TakeoverAdmission, owner: UUID, kind: str) -> str:
    """
    创建真实凭据，不替换 HTTP 授权依赖。

    :param admission (TakeoverAdmission): 数据库与节点身份
    :param owner (UUID): 令牌所有者
    :param kind (str): user、device 或 node
    :return str: 仅供测试使用的凭据
    """
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from agent_remote_server.models import AuthToken, UserDevice
    from agent_remote_server.security.tokens import hash_token

    state = admission.state
    value = f"takeover-admission-test:{uuid4()}"
    async with state.library.database.begin() as session:
        hashed = hash_token(state.settings.secret_key, value)
        if kind == "node":
            node = await session.get(Node, state.node)
            assert node is not None
            node.node_token_hash = hashed
        else:
            device_id = None
            if kind == "device":
                device = UserDevice(user_id=owner, name="测试", platform="linux", status="active")
                session.add(device)
                await session.flush()
                device_id = device.id
            session.add(
                AuthToken(
                    user_id=owner,
                    user_device_id=device_id,
                    token_type=kind,
                    token_hash=hashed,
                    status="active",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    return value
