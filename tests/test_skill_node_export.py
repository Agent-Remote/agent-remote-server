"""
验证配额外只读导出的在线授权、跨用户隔离、固定快照与撤销边界。
"""

import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_node_export_support import ExportHarness
from skill_node_export_support import export_client as export_client
from skill_node_export_support import export_state as export_state
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import AuthToken, Node, NodeTask, SshKey, User, UserDevice
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStorageUsage
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.schemas.skill_node_export import (
    NodeExportAuthorization,
    NodeExportVerification,
)
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.node_export import NodeExportService
from agent_remote_server.services.skills.node_export_tokens import NodeExportTokens


async def authorize(state: ExportHarness) -> NodeExportAuthorization:
    """
    通过新的事务签发并提交一次授权及幂等公钥同步。

    :param state (ExportHarness): 原始身份
    :return NodeExportAuthorization: 原始固定连接授权
    """
    async with state.state.database.begin() as session:
        return await NodeExportService(session, state.settings).authorize(
            state.state.owner, state.token_id, state.state.snapshot, state.request
        )


async def test_authorization_requires_no_upload_quota_capability_or_stopped_claim(
    export_state: ExportHarness, export_client: AsyncClient
) -> None:
    """
    原 Node 尚未报告冻结且配额耗尽也能取得读授权，但不能宣称内容可用或完成。

    :param export_state (ExportHarness): 无实际上传的原快照
    :param export_client (AsyncClient): 真实用户 HTTP 客户端
    """
    state = export_state.state
    async with state.database.begin() as session:
        usage = await session.get(SkillStorageUsage, state.owner)
        assert usage is not None
        usage.state_bytes = 10**15
        uploads = await session.scalar(select(func.count()).select_from(SkillContentUpload))
    response = await export_client.post(
        f"/api/v1/skills/state/node-exports/{state.snapshot}/authorize",
        json=export_state.request.model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "authorized" and not body["committed"]
    auth = NodeExportAuthorization.model_validate(body["data"])
    assert auth.binding.snapshot_id == state.snapshot and auth.binding.node_id == state.node
    assert auth.authorization_task_status == "pending"
    verification = await export_client.post(
        f"/api/v1/node/skill-state-exports/{state.snapshot}/verify",
        headers={"Authorization": "Bearer " + export_state.node_token},
        json=export_state.request.model_dump(mode="json") | {"grant": auth.grant},
    )
    assert verification.status_code == 200, verification.text
    permission = verification.json()["data"]
    assert permission["binding"] == body["data"]["binding"]
    assert permission["incoming_digest"] is None and permission["unclean"] is None
    assert permission["recheck_seconds"] == 10
    async with state.database() as session:
        assert await session.scalar(select(func.count()).select_from(SkillFinalization)) == 0
        assert await session.scalar(select(func.count()).select_from(SkillContentUpload)) == uploads
        tasks = (
            await session.scalars(select(NodeTask).where(NodeTask.task_type == "sync_ssh_keys"))
        ).all()
        assert len(tasks) == 1 and tasks[0].task_id == auth.authorization_task_id
        assert auth.grant not in str(tasks[0].payload)


@pytest.mark.parametrize(
    "changed",
    [
        "user",
        "token",
        "token_type",
        "expiry",
        "device",
        "key",
        "key_device",
        "snapshot_epoch",
        "snapshot_generation",
        "tampered_grant",
    ],
)
@pytest.mark.parametrize("renewal", [False, True])
async def test_original_grant_cannot_survive_revoked_or_changed_authority(
    export_state: ExportHarness, changed: str, renewal: bool
) -> None:
    """
    已签名凭据不能掩盖原用户、设备、密钥和快照权限的后续变化。

    :param export_state (ExportHarness): 原授权身份
    :param changed (str): 被撤销或替换的权限事实
    :param renewal (bool): 是否检查独立续授权入口
    """
    auth = await authorize(export_state)
    state = export_state.state
    async with state.database.begin() as session:
        match changed:
            case "user":
                owner = await session.get(User, state.owner)
                assert owner is not None
                owner.status = "disabled"
            case "token" | "token_type" | "expiry":
                token = await session.get(AuthToken, export_state.token_id)
                assert token is not None
                if changed == "token":
                    token.status = "revoked"
                elif changed == "token_type":
                    token.token_type = "device"
                else:
                    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            case "device":
                device = await session.get(UserDevice, export_state.request.device_id)
                assert device is not None
                device.status = "revoked"
            case "key" | "key_device":
                key = await session.get(SshKey, export_state.request.ssh_key_id)
                assert key is not None
                if changed == "key":
                    key.status = "revoked"
                else:
                    other = UserDevice(
                        user_id=state.owner, name="另一设备", platform="linux", status="active"
                    )
                    session.add(other)
                    await session.flush()
                    key.user_device_id = other.id
            case _:
                snapshot = await session.get(SessionSkillSnapshot, state.snapshot)
                assert snapshot is not None
                if changed == "snapshot_epoch":
                    snapshot.directory_epoch += 1
                elif changed == "snapshot_generation":
                    snapshot.library_generation += 1
                else:
                    auth = auth.model_copy(update={"grant": auth.grant + "changed"})
    request = NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant)
    async with state.database() as session:
        with pytest.raises(SkillContentError, match="authorization denied"):
            service = NodeExportService(session, export_state.settings)
            operation = service.renew if renewal else service.verify
            await operation(state.node, state.snapshot, request)


@pytest.mark.parametrize("wrong", ["node", "snapshot", "device", "key"])
@pytest.mark.parametrize("renewal", [False, True])
async def test_grant_rejects_foreign_forced_command_context(
    export_state: ExportHarness, wrong: str, renewal: bool
) -> None:
    """
    凭据即使完整也不能在其他节点、快照或 SSH 强制身份中重放。

    :param export_state (ExportHarness): 原始授权
    :param wrong (str): 请求替换的上下文
    :param renewal (bool): 是否检查独立续授权入口
    """
    auth = await authorize(export_state)
    request = NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant)
    node_id, snapshot_id = export_state.state.node, export_state.state.snapshot
    if wrong == "node":
        node_id = uuid4()
    elif wrong == "snapshot":
        snapshot_id = uuid4()
    else:
        request = request.model_copy(
            update={"device_id" if wrong == "device" else "ssh_key_id": uuid4()}
        )
    async with export_state.state.database() as session:
        with pytest.raises(SkillContentError):
            service = NodeExportService(session, export_state.settings)
            operation = service.renew if renewal else service.verify
            await operation(node_id, snapshot_id, request)


async def test_known_termination_is_returned_without_starting_an_upload(
    export_state: ExportHarness,
) -> None:
    """
    已知冻结事实必须固定传输输入，返回真实摘要不伪造检查点或发布。

    :param export_state (ExportHarness): 原始快照
    """
    auth = await authorize(export_state)
    state = export_state.state
    async with state.database.begin() as session:
        session.add(
            SkillSnapshotTermination(
                snapshot_id=state.snapshot, incoming_digest="a" * 64, unclean=True
            )
        )
    async with state.database() as session:
        result = await NodeExportService(session, export_state.settings).verify(
            state.node,
            state.snapshot,
            NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant),
        )
        assert result.incoming_digest == "a" * 64 and result.unclean is True


async def test_foreign_owner_and_device_token_cannot_authorize(
    export_state: ExportHarness, export_client: AsyncClient
) -> None:
    """
    登录和设备身份不能借用其他用户快照，拒绝时不创建同步任务。

    :param export_state (ExportHarness): 原始所有者
    :param export_client (AsyncClient): 真实认证请求
    """
    state = export_state.state
    other = await user(state.database)
    async with state.database.begin() as session:
        token = await session.get(AuthToken, export_state.token_id)
        assert token is not None
        token.user_id = other
    response = await export_client.post(
        f"/api/v1/skills/state/node-exports/{state.snapshot}/authorize",
        json=export_state.request.model_dump(mode="json"),
    )
    assert (
        response.status_code == 409
        and response.json()["errors"][0]["code"] == "STATE_EXPORT_DENIED"
    )
    async with state.database.begin() as session:
        token = await session.get(AuthToken, export_state.token_id)
        assert token is not None
        token.user_id, token.token_type = state.owner, "device"
        token.user_device_id = export_state.request.device_id
    response = await export_client.post(
        f"/api/v1/skills/state/node-exports/{state.snapshot}/authorize",
        json=export_state.request.model_dump(mode="json"),
    )
    assert (
        response.status_code == 403
        and response.json()["errors"][0]["code"] == "USER_TOKEN_REQUIRED"
    )
    async with state.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTask)
                .where(NodeTask.task_type == "sync_ssh_keys")
            )
            == 0
        )


async def test_repeated_concurrent_authorization_reuses_key_task(
    export_state: ExportHarness,
) -> None:
    """
    不同短期导出授权共享既有密钥同步任务，不创建重复运行任务或续写内容。

    :param export_state (ExportHarness): 固定设备及原始快照
    """
    first, second = await asyncio.gather(authorize(export_state), authorize(export_state))
    assert first.authorization_task_id == second.authorization_task_id
    assert first.grant != second.grant and first.binding == second.binding
    async with export_state.state.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTask)
                .where(NodeTask.task_type == "sync_ssh_keys")
            )
            == 1
        )


async def test_grant_has_fixed_bounded_lifetime_and_separate_domain(
    export_state: ExportHarness,
) -> None:
    """
    时间到期、未来凭据、超长寿命及其他协议密钥域都不能授权读取。

    :param export_state (ExportHarness): 原始用户授权
    """
    auth = await authorize(export_state)
    tokens = NodeExportTokens(export_state.settings.secret_key)
    now = int(datetime.now(UTC).timestamp())
    grant = tokens.verify(auth.grant, now)
    assert grant.expires_at - grant.issued_at == 900
    for moment in (grant.issued_at - 1, grant.expires_at):
        with pytest.raises(SkillContentError):
            tokens.verify(auth.grant, moment)
    with pytest.raises(SkillContentError):
        tokens.verify(
            tokens.sign(grant.model_copy(update={"expires_at": grant.issued_at + 901})), now
        )
    with pytest.raises(SkillContentError):
        NodeExportTokens("another-secret").verify(auth.grant, now)

    payload = auth.grant.split(".")[0]
    signature = hmac.new(
        export_state.settings.secret_key.encode(),
        b"agent-remote:another-protocol:v1\x00" + payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(SkillContentError):
        tokens.verify(payload + "." + signature, now)


async def test_grant_cannot_outlive_user_token_or_replace_leased_key_task(
    export_state: ExportHarness,
) -> None:
    """
    剩余用户时限约束授权，已领取密钥任务必须保持原身份和状态。

    :param export_state (ExportHarness): 原始授权身份
    """
    auth = await authorize(export_state)
    expiry = datetime.now(UTC) + timedelta(seconds=45)
    async with export_state.state.database.begin() as session:
        token = await session.get(AuthToken, export_state.token_id)
        assert token is not None
        token.expires_at = expiry
        task = await session.scalar(
            select(NodeTask).where(NodeTask.task_id == auth.authorization_task_id)
        )
        assert task is not None
        task.status = "leased"
    renewed = await authorize(export_state)
    assert renewed.authorization_task_status == "leased"
    assert renewed.authorization_task_id == auth.authorization_task_id
    assert int(renewed.expires_at.timestamp()) == int(expiry.timestamp())


@pytest.mark.parametrize("fault", ["disabled", "offline", "host", "username"])
async def test_unavailable_source_cannot_create_export_authority(
    export_state: ExportHarness, fault: str
) -> None:
    """
    功能开关和节点地址验证在密钥任务和授权签发之前生效。

    :param export_state (ExportHarness): 原始授权身份
    :param fault (str): 失效的功能或节点条件
    """
    if fault == "disabled":
        export_state.settings.skill_manager_enabled = False
    else:
        async with export_state.state.database.begin() as session:
            node = await session.get(Node, export_state.state.node)
            assert node is not None
            if fault == "offline":
                node.status = "offline"
            elif fault == "host":
                node.wireguard_ip, node.ssh_host = None, "-oProxyCommand=private"
            else:
                node.ssh_user = "root;private"
    with pytest.raises(SkillContentError):
        await authorize(export_state)
    async with export_state.state.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTask)
                .where(NodeTask.task_type == "sync_ssh_keys")
            )
            == 0
        )


@pytest.mark.parametrize("renewal", [False, True])
async def test_conflicting_saved_observations_cannot_authorize_capture(
    export_state: ExportHarness, renewal: bool
) -> None:
    """
    矛盾的原终止和上传摘要必须拒绝，不能任选一个继续传输。

    :param export_state (ExportHarness): 原始授权身份
    :param renewal (bool): 是否检查独立续授权入口
    """
    auth = await authorize(export_state)
    state = export_state.state
    async with state.database.begin() as session:
        session.add(
            SkillSnapshotTermination(
                snapshot_id=state.snapshot, incoming_digest="a" * 64, unclean=False
            )
        )
        session.add(
            SkillFinalization(
                user_id=state.owner,
                account_id=state.account,
                node_id=state.node,
                snapshot_id=state.snapshot,
                idempotency_key=str(uuid4()),
                request_digest="c" * 64,
                incoming_digest="b" * 64,
                unclean=False,
            )
        )
    async with state.database() as session:
        with pytest.raises(SkillContentError):
            service = NodeExportService(session, export_state.settings)
            operation = service.renew if renewal else service.verify
            await operation(
                state.node,
                state.snapshot,
                NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant),
            )


@pytest.mark.parametrize("fault", ["unknown", "oversized", "null", "alias", "malformed"])
@pytest.mark.parametrize("operation", ["verify", "renew"])
async def test_invalid_verification_never_echoes_grant(
    export_state: ExportHarness, export_client: AsyncClient, fault: str, operation: str
) -> None:
    """
    请求字段和 JSON 错误不允许框架把原短期凭据放入公开响应。

    :param export_state (ExportHarness): 原始授权身份
    :param export_client (AsyncClient): 真实认证 HTTP 客户端
    :param fault (str): 无效载荷的种类
    :param operation (str): 固定验证或续签路由
    """
    auth = await authorize(export_state)
    body: dict[str, object] = export_state.request.model_dump(mode="json") | {"grant": auth.grant}
    if fault == "unknown":
        body["private"] = auth.grant
    elif fault == "oversized":
        body["grant"] = auth.grant + "x" * 4096
    elif fault == "null":
        body["grant"] = None
    elif fault == "alias":
        body["Grant"] = body.pop("grant")
    if fault == "malformed":
        response = await export_client.post(
            f"/api/v1/node/skill-state-exports/{export_state.state.snapshot}/{operation}",
            headers={
                "Authorization": "Bearer " + export_state.node_token,
                "Content-Type": "application/json",
            },
            content='{"grant":"' + auth.grant,
        )
    else:
        response = await export_client.post(
            f"/api/v1/node/skill-state-exports/{export_state.state.snapshot}/{operation}",
            headers={"Authorization": "Bearer " + export_state.node_token},
            json=body,
        )
    assert response.status_code == 422
    assert auth.grant not in response.text
    assert response.json()["errors"][0]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("operation", ["verify", "renew"])
async def test_node_verification_requires_live_original_node_token(
    export_state: ExportHarness, export_client: AsyncClient, operation: str
) -> None:
    """
    用户凭据不能替代节点凭据，被禁用的原节点也不能重验。

    :param export_state (ExportHarness): 原始授权身份
    :param export_client (AsyncClient): 真实认证 HTTP 客户端
    :param operation (str): 固定验证或续签路由
    """
    auth = await authorize(export_state)
    route = f"/api/v1/node/skill-state-exports/{export_state.state.snapshot}/{operation}"
    body = export_state.request.model_dump(mode="json") | {"grant": auth.grant}
    response = await export_client.post(route, json=body)
    assert response.status_code in {401, 403}
    async with export_state.state.database.begin() as session:
        node = await session.get(Node, export_state.state.node)
        assert node is not None
        node.status = "disabled"
    response = await export_client.post(
        route, headers={"Authorization": "Bearer " + export_state.node_token}, json=body
    )
    assert response.status_code in {401, 403}
