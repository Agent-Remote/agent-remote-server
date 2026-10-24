"""
验证设备授权绑定连接固定原尝试并保持只读轮询和禁用状态。
"""

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from test_tool_accounts_api import (
    auth_header,
    bootstrap,
    create_and_register_node,
    create_tool_account,
)
from test_tool_accounts_api import client as client

from agent_remote_server.models import (
    Node,
    NodeTask,
    SshKey,
    ToolAccount,
    ToolAccountProfile,
    User,
    UserDevice,
)


@dataclass
class BindingFixture:
    """
    保存隔离绑定及其两种本机凭据。
    """

    account: str
    binding: str
    node: str
    node_token: str
    device: str
    token: str
    user_token: str
    ssh_key: str

    @property
    def path(self) -> str:
        """
        返回固定账户连接路径。

        :return str: 连接路径
        """
        return f"/api/v1/tool-accounts/{self.account}/bind/attach"


def binding_fixture(client: TestClient, backend: str = "docker_sandbox") -> BindingFixture:
    """
    创建隔离账户并固定一个可连接绑定尝试。

    :param client (TestClient): 隔离客户端
    :param backend (str): 运行时后端
    :return BindingFixture: 绑定测试上下文
    """
    token = bootstrap(client)
    node, node_token = create_and_register_node(client, token)
    device_response = client.post(
        "/api/v1/devices/register",
        headers=auth_header(token),
        json={
            "name": "binding-fixture",
            "platform": "linux",
            "ssh_public_key": "ssh-ed25519 AAAABINDING synthetic@test",
            "wireguard_public_key": "synthetic-binding-wireguard",
        },
    )
    assert device_response.status_code == 200
    device = device_response.json()["data"]
    account_id = str(create_tool_account(client, token)["id"])
    binding = "bind-exact-original"

    async def prepare() -> None:
        """
        固定测试绑定和运行时，不运行节点任务。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            account = await session.get(ToolAccount, UUID(account_id))
            assert account is not None
            assigned_node = await session.get(Node, UUID(node))
            assert assigned_node is not None
            assigned_node.runtime_capabilities = {
                **assigned_node.runtime_capabilities,
                "binding_attach": {"supported": True, "protocol_version": 1},
            }
            account.status = "binding_waiting_user_login"
            account.affinity_node_id = UUID(node)
            account.runtime_backend = backend
            profile = await session.scalar(
                select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == account.id)
            )
            assert profile is not None
            profile.profile_json = {
                **profile.profile_json,
                "binding_session_id": binding,
                "tmux_session_name": "binding-original",
            }
            await session.commit()

    asyncio.run(prepare())
    return BindingFixture(
        account_id,
        binding,
        node,
        node_token,
        device["device"]["id"],
        device["device_token"]["access_token"],
        token,
        device["ssh_key_id"],
    )


def task_snapshot(client: TestClient) -> list[tuple[str, str]]:
    """
    读取任务身份与状态以证明轮询无任务变更。

    :param client (TestClient): 隔离客户端
    :return list[tuple[str, str]]: 原任务快照
    """

    async def read() -> list[tuple[str, str]]:
        """
        读取任务快照。

        :return list[tuple[str, str]]: 任务快照
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            tasks = (await session.scalars(select(NodeTask).order_by(NodeTask.task_id))).all()
            return [(task.task_id, task.status) for task in tasks]

    return asyncio.run(read())


@pytest.mark.parametrize("backend", ["native", "docker_sandbox"])
def test_binding_attach_authorizes_exact_attempt_and_polls_read_only(
    client: TestClient, backend: str
) -> None:
    """
    两个后端同步原设备密钥且 GET 仅观察原任务。

    :param client (TestClient): 隔离客户端
    :param backend (str): 运行时后端
    """
    fixture = binding_fixture(client, backend)
    request = {"expected_binding_session_id": fixture.binding}
    response = client.post(fixture.path, headers=auth_header(fixture.token), json=request)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == {
        "version": 1,
        "tool_account_id": fixture.account,
        "binding_session_id": fixture.binding,
        "node_id": fixture.node,
        "device_id": fixture.device,
        "ssh_host": "10.42.0.10",
        "ssh_port": 22,
        "ssh_user": "agent-remote",
        "tmux_session_name": "binding-original",
        "authorization_task_id": data["authorization_task_id"],
        "authorization_task_status": "pending",
        "expires_in": 300,
    }
    before = task_snapshot(client)
    assert len(before) == 1
    for _ in range(2):
        poll = client.get(
            f"{fixture.path}/{data['authorization_task_id']}",
            headers=auth_header(fixture.token),
            params=request,
        )
        assert poll.status_code == 200, poll.text
        assert poll.json()["data"] == data
    assert task_snapshot(client) == before


@pytest.mark.parametrize("invalid", ["wrong_generation", "user_token", "unknown_field"])
def test_binding_attach_rejects_before_task_creation(client: TestClient, invalid: str) -> None:
    """
    拒绝未绑定设备、旧尝试和额外请求字段且无任务副作用。

    :param client (TestClient): 隔离客户端
    :param invalid (str): 无效输入类别
    """
    fixture = binding_fixture(client)
    request = {"expected_binding_session_id": fixture.binding}
    if invalid == "wrong_generation":
        request["expected_binding_session_id"] = "bind-newer"
    if invalid == "unknown_field":
        request["command"] = "untrusted"
    token = fixture.user_token if invalid == "user_token" else fixture.token
    response = client.post(fixture.path, headers=auth_header(token), json=request)
    assert response.status_code in {403, 409, 422}
    assert task_snapshot(client) == []


@pytest.mark.parametrize(
    "change",
    [
        "task_failed",
        "task_payload",
        "task_type",
        "task_missing",
        "binding",
        "disabled",
        "node_offline",
        "key_changed",
        "device_revoked",
        "owner",
    ],
)
def test_binding_poll_never_repairs_changed_authority(client: TestClient, change: str) -> None:
    """
    已派发任务的轮询不重置失败或绕过变更后的授权。

    :param client (TestClient): 隔离客户端
    :param change (str): 任务或账户变更
    """
    fixture = binding_fixture(client)
    request = {"expected_binding_session_id": fixture.binding}
    response = client.post(fixture.path, headers=auth_header(fixture.token), json=request)
    assert response.status_code == 200
    task_id = response.json()["data"]["authorization_task_id"]

    async def mutate() -> None:
        """
        模拟原任务终态或已变化的账户事实。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
            account = await session.get(ToolAccount, UUID(fixture.account))
            assert task is not None and account is not None
            if change == "task_failed":
                task.status = "failed"
            elif change == "task_payload":
                task.payload = {**task.payload, "device_id": str(UUID(int=99))}
            elif change == "task_type":
                task.task_type = "verify_tool_account"
            elif change == "task_missing":
                await session.delete(task)
            elif change == "disabled":
                account.status = "disabled"
            elif change == "node_offline":
                node = await session.get(Node, UUID(fixture.node))
                assert node is not None
                node.status = "offline"
            elif change == "key_changed":
                key = await session.scalar(
                    select(SshKey).where(SshKey.user_device_id == UUID(fixture.device))
                )
                assert key is not None
                key.public_key = "ssh-ed25519 AAAANEW synthetic@test"
            elif change == "device_revoked":
                device = await session.get(UserDevice, UUID(fixture.device))
                assert device is not None
                device.status = "revoked"
            elif change == "owner":
                user = User(
                    username="other",
                    display_name="Other",
                    role="user",
                    status="active",
                    password_hash="unused-synthetic-hash",
                )
                session.add(user)
                await session.flush()
                account.user_id = user.id
            else:
                profile = await session.scalar(
                    select(ToolAccountProfile).where(
                        ToolAccountProfile.tool_account_id == account.id
                    )
                )
                assert profile is not None
                profile.profile_json = {**profile.profile_json, "binding_session_id": "bind-new"}
            await session.commit()

    asyncio.run(mutate())
    before = task_snapshot(client)
    poll = client.get(
        f"{fixture.path}/{task_id}", headers=auth_header(fixture.token), params=request
    )
    if change == "task_failed":
        assert poll.status_code == 200
        assert poll.json()["data"]["authorization_task_status"] == "failed"
    elif change in {"task_missing", "owner"}:
        assert poll.status_code == 404
    elif change == "device_revoked":
        assert poll.status_code == 403
    else:
        assert poll.status_code == 409
    assert task_snapshot(client) == before


def test_binding_poll_rejects_other_device_original_task(client: TestClient) -> None:
    """
    同用户其他设备不能将原设备同步任务当成自己的授权。

    :param client (TestClient): 隔离客户端
    """
    fixture = binding_fixture(client)
    request = {"expected_binding_session_id": fixture.binding}
    response = client.post(fixture.path, headers=auth_header(fixture.token), json=request)
    assert response.status_code == 200
    task_id = response.json()["data"]["authorization_task_id"]
    other = client.post(
        "/api/v1/devices/register",
        headers=auth_header(fixture.user_token),
        json={
            "name": "other-device",
            "platform": "linux",
            "ssh_public_key": "ssh-ed25519 AAAAOTHER synthetic@test",
            "wireguard_public_key": "synthetic-other-wireguard",
        },
    )
    assert other.status_code == 200
    token = other.json()["data"]["device_token"]["access_token"]
    before = task_snapshot(client)
    poll = client.get(f"{fixture.path}/{task_id}", headers=auth_header(token), params=request)
    assert poll.status_code == 409
    assert task_snapshot(client) == before


def test_node_binding_verification_enforces_expected_attempt(client: TestClient) -> None:
    """
    节点接口保持旧请求兼容但拒绝新请求的错误尝试。

    :param client (TestClient): 隔离客户端
    """
    fixture = binding_fixture(client)
    request = {
        "node_id": fixture.node,
        "tool_account_id": fixture.account,
        "device_id": fixture.device,
    }
    for expected, status in [(None, 200), (fixture.binding, 200), ("bind-wrong", 409)]:
        body = dict(request)
        if expected is not None:
            body["expected_binding_session_id"] = expected
            body["ssh_key_id"] = fixture.ssh_key
        response = client.post(
            "/api/v1/node-api/binding-attach/verify",
            headers=auth_header(fixture.node_token),
            json=body,
        )
        assert response.status_code == status
        if status == 200:
            assert response.json()["data"]["ssh_key_id"] == (
                fixture.ssh_key if expected is not None else None
            )


@pytest.mark.parametrize("key_state", ["missing", "unknown", "revoked", "other_device"])
def test_exact_node_binding_rejects_unverified_ssh_key(client: TestClient, key_state: str) -> None:
    """
    精确连接拒绝无身份、已撤销和其他设备的 SSH 密钥。

    :param client (TestClient): 隔离客户端
    :param key_state (str): 不可信密钥状态
    """
    fixture = binding_fixture(client)
    key_id = fixture.ssh_key
    if key_state == "unknown":
        key_id = str(UUID(int=99))
    if key_state == "other_device":
        other = client.post(
            "/api/v1/devices/register",
            headers=auth_header(fixture.user_token),
            json={
                "name": "key-owner",
                "platform": "linux",
                "ssh_public_key": "ssh-ed25519 AAAAOTHER synthetic@test",
            },
        )
        assert other.status_code == 200
        key_id = other.json()["data"]["ssh_key_id"]
    if key_state == "revoked":

        async def revoke() -> None:
            """
            模拟仅撤销 SSH 密钥而设备继续活跃。
            """
            app = cast(FastAPI, client.app)
            async with app.state.session_factory() as session:
                key = await session.get(SshKey, UUID(fixture.ssh_key))
                assert key is not None
                key.status = "revoked"
                await session.commit()

        asyncio.run(revoke())
    body = {
        "node_id": fixture.node,
        "tool_account_id": fixture.account,
        "device_id": fixture.device,
        "expected_binding_session_id": fixture.binding,
    }
    if key_state != "missing":
        body["ssh_key_id"] = key_id
    response = client.post(
        "/api/v1/node-api/binding-attach/verify",
        headers=auth_header(fixture.node_token),
        json=body,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SSH_KEY_REVOKED"


@pytest.mark.parametrize("kind", ["create_binding_session", "verify_tool_account"])
@pytest.mark.parametrize("outcome", ["complete", "fail"])
@pytest.mark.parametrize("rebind", [False, True])
def test_disabled_account_survives_late_binding_results(
    client: TestClient, kind: str, outcome: str, rebind: bool
) -> None:
    """
    禁用后到达的绑定与校验成功或失败均不能重新开放账户。

    :param client (TestClient): 隔离客户端
    :param kind (str): 原任务类型
    :param outcome (str): 节点终态报告
    :param rebind (bool): 禁用后是否显式开始新绑定
    """
    fixture = binding_fixture(client)
    task_id = f"{kind}:{fixture.account}:original"

    async def prepare() -> None:
        """
        建立已派发任务与当前 profile 的精确绑定。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            profile = await session.scalar(
                select(ToolAccountProfile).where(
                    ToolAccountProfile.tool_account_id == UUID(fixture.account)
                )
            )
            assert profile is not None
            key = "binding_task_id" if kind == "create_binding_session" else "verification_task_id"
            profile.profile_json = {**profile.profile_json, key: task_id}
            session.add(
                NodeTask(
                    node_id=UUID(fixture.node),
                    task_id=task_id,
                    task_type=kind,
                    status="pending",
                    payload={"tool_account_id": fixture.account},
                    retry_count=0,
                )
            )
            await session.commit()

    asyncio.run(prepare())
    disabled = client.post(
        f"/api/v1/tool-accounts/{fixture.account}/disable", headers=auth_header(fixture.token)
    )
    assert disabled.status_code == 200
    verify = client.post(
        f"/api/v1/tool-accounts/{fixture.account}/bind/verify", headers=auth_header(fixture.token)
    )
    assert verify.status_code == 409
    expected_binding = fixture.binding
    if rebind:
        restarted = client.post(
            f"/api/v1/tool-accounts/{fixture.account}/bind/start",
            headers=auth_header(fixture.token),
        )
        assert restarted.status_code == 200, restarted.text
        expected_binding = restarted.json()["data"]["binding_session_id"]
        assert expected_binding != fixture.binding
    body = (
        {
            "result": {
                "tool_account_id": fixture.account,
                "status": "ready",
                "verified": True,
                "binding_session_id": fixture.binding,
                "tmux_session_name": "binding-original",
            }
        }
        if outcome == "complete"
        else {"error": {"message": "synthetic failure"}}
    )
    for _ in range(2):
        completed = client.post(
            f"/api/v1/node-api/tasks/{task_id}/{outcome}",
            headers=auth_header(fixture.node_token),
            json=body,
        )
        assert completed.status_code == 200, completed.text
    status = client.get(
        f"/api/v1/tool-accounts/{fixture.account}", headers=auth_header(fixture.token)
    )
    assert status.json()["data"]["status"] == ("binding_session_starting" if rebind else "disabled")
    binding_status = client.get(
        f"/api/v1/tool-accounts/{fixture.account}/bind/status",
        headers=auth_header(fixture.token),
    )
    assert binding_status.status_code == 200
    assert binding_status.json()["data"]["binding_session_id"] == expected_binding
    assert len(task_snapshot(client)) == (2 if rebind else 1)


@pytest.mark.parametrize("method", ["POST", "GET"])
@pytest.mark.parametrize(
    "capability",
    [
        None,
        {},
        {"supported": False, "protocol_version": 1},
        {"supported": True, "protocol_version": 2},
        {"supported": True, "protocol_version": True},
        {"supported": True, "protocol_version": 1.0},
        {"supported": True, "protocol_version": "1"},
        {"supported": "true", "protocol_version": 1},
        {"supported": 1, "protocol_version": 1},
        {"supported": True},
        "unsupported",
    ],
)
def test_binding_attach_requires_verified_gateway_capability(
    client: TestClient, method: str, capability: object
) -> None:
    """
    未探测或不兼容的已安装网关不能创建授权任务或继续观察。

    :param client (TestClient): 隔离客户端
    :param method (str): 请求方法
    :param capability (object): 不受信任的节点能力值
    """
    fixture = binding_fixture(client)
    request = {"expected_binding_session_id": fixture.binding}
    path = fixture.path
    if method == "GET":
        authorized = client.post(path, headers=auth_header(fixture.token), json=request)
        assert authorized.status_code == 200
        path += "/" + authorized.json()["data"]["authorization_task_id"]
    before = task_snapshot(client)

    async def remove_support() -> None:
        """
        模拟旧网关或升级失败后的节点能力报告。
        """
        app = cast(FastAPI, client.app)
        async with app.state.session_factory() as session:
            node = await session.get(Node, UUID(fixture.node))
            assert node is not None
            values = dict(node.runtime_capabilities)
            values.pop("binding_attach", None)
            if capability is not None:
                values["binding_attach"] = capability
            node.runtime_capabilities = values
            # JSON 类型变化不能被 Python 的 True == 1 字典比较吞掉。
            flag_modified(node, "runtime_capabilities")
            await session.commit()

    asyncio.run(remove_support())
    if method == "POST":
        response = client.post(path, headers=auth_header(fixture.token), json=request)
    else:
        response = client.get(path, headers=auth_header(fixture.token), params=request)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NODE_BINDING_ATTACH_UNSUPPORTED"
    assert task_snapshot(client) == before


@pytest.mark.parametrize(
    "capability",
    [
        {"supported": 1, "protocol_version": 1},
        {"supported": True, "protocol_version": True},
        {"supported": True, "protocol_version": 1.0},
    ],
)
def test_binding_gateway_heartbeat_type_drift_revokes_previous_support(
    client: TestClient, capability: dict[str, object]
) -> None:
    """
    即使布尔与数字比较相等，后续错误报告也必须清除原能力。

    :param client (TestClient): 隔离客户端
    :param capability (dict[str, object]): 错误类型的网关能力
    """
    fixture = binding_fixture(client)
    for supported in [{"supported": True, "protocol_version": 1}, capability]:
        heartbeat = client.post(
            "/api/v1/node-api/heartbeat",
            headers=auth_header(fixture.node_token),
            json={
                "node_id": fixture.node,
                "version": "test",
                "supported_tool_types": ["claude"],
                "resources": {
                    "cpu_load": 0.0,
                    "memory_used_bytes": 0,
                    "memory_total_bytes": 1,
                    "disk_used_bytes": 0,
                    "disk_total_bytes": 1,
                },
                "runtime": {
                    "docker_ok": True,
                    "tmux_ok": True,
                    "runtime_capabilities": {
                        "backends": ["docker_sandbox"],
                        "binding_attach": supported,
                    },
                },
            },
        )
        assert heartbeat.status_code == 200, heartbeat.text
    response = client.post(
        fixture.path,
        headers=auth_header(fixture.token),
        json={"expected_binding_session_id": fixture.binding},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NODE_BINDING_ATTACH_UNSUPPORTED"
    assert task_snapshot(client) == []
