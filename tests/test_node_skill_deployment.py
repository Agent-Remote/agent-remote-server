"""
验证部署内容使用真实 Node 凭据、精确领取轮次和完整私有文件传输。
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from test_skill_content_service import database as database
from test_skill_deployment_authorization import leased
from test_skill_deployment_dispatch import settings
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.api import node_skill_deployment
from agent_remote_server.api.deps import get_session
from agent_remote_server.main import create_app
from agent_remote_server.models import Node, NodeTask
from agent_remote_server.schemas.skill_deployment_content import SkillDeploymentIdentity
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.security.tokens import hash_token
from agent_remote_server.services.skills.deployment_content import (
    DeploymentFileDownload,
    NodeDeploymentContent,
)


@pytest.fixture
async def deployment_client(prepared: RuntimeHarness, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """
    用真实预约创建输入，HTTP 请求独立取得数据库连接并走生产节点令牌校验。

    :param prepared (RuntimeHarness): 真实账户和内容卷身份
    :param tmp_path (Path): 私有测试卷
    :return AsyncIterator[AsyncClient]: 指向精确部署资源的认证客户端
    """
    binding = await leased(prepared, tmp_path)
    configured = settings(tmp_path)
    configured.log_level = "CRITICAL"
    token = "deployment-test:" + str(prepared.node)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        node.node_token_hash = hash_token(configured.secret_key, token)
    app = create_app(configured)

    async def request_session() -> AsyncIterator[AsyncSession]:
        """
        各个 HTTP 请求独立提交或回滚。

        :return AsyncIterator[AsyncSession]: 当前请求事务
        """
        async with prepared.database() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"http://test/api/v1/node/skill-deployments/{binding.attempt_id}/",
        headers={"Authorization": "Bearer " + token},
        params={"task_id": str(binding.task_id), "lease_attempt": "1"},
    ) as client:
        yield client
    await app.state.database_engine.dispose()


async def test_complete_plan_manifest_and_all_file_bytes(deployment_client: AsyncClient) -> None:
    """
    清单返回原计划和完整账户内容，准备可下载不伪装为 Node 就绪。

    :param deployment_client (AsyncClient): 真实认证客户端
    """
    response = await deployment_client.get(str(deployment_client.base_url).rstrip("/"))
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["status"] == "prepared_input" and not envelope["committed"]
    data = envelope["data"]
    assert data["plan"]["operation_id"] == data["operation_id"]
    assert {item["entry_name"] for item in data["items"]} == {"learning", "notes"}
    for entry in data["manifest"]["entries"]:
        if entry["kind"] != "file":
            continue
        downloaded = await deployment_client.get("files/" + entry["sha256"])
        assert downloaded.status_code == 200
        assert len(downloaded.content) == entry["size"]
        assert downloaded.headers["etag"] == '"' + entry["sha256"] + '"'


@pytest.mark.parametrize("attempt", [0, -1, 2, 2147483648, "true"])
async def test_wrong_poll_attempt_cannot_read_manifest_or_file(
    deployment_client: AsyncClient, attempt: int | str
) -> None:
    """
    请求必须属于原领取，合法摘要和节点令牌不能替代轮次身份。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param attempt (int | str): 无效或过期领取身份
    """
    base = str(deployment_client.base_url).rstrip("/")
    original = (await deployment_client.get(base)).json()["data"]
    file = next(entry for entry in original["manifest"]["entries"] if entry["kind"] == "file")
    for path in (base, base + "/files/" + file["sha256"]):
        denied = await deployment_client.get(path, params={"lease_attempt": attempt})
        assert denied.status_code in {409, 422}
        assert b"name: learning" not in denied.content


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"lease_attempt": True},
        {"lease_attempt": "1"},
        {"lease_attempt": 1.0},
        {"lease_attempt": 0},
        {"lease_attempt": 1, "tree_digest": "override"},
    ],
)
async def test_lease_body_rejects_ambiguous_or_extra_fields(
    deployment_client: AsyncClient, body: dict[str, object]
) -> None:
    """
    续租请求不可用宽松数值转换或多余输入更换原权限。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param body (dict[str, object]): 不受信请求
    """
    assert (await deployment_client.post("lease", json=body)).status_code == 422


async def test_renewal_echoes_original_input_and_cannot_revive_expired_lease(
    deployment_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    短期续租回显完整绑定；到期以后读取及续租均失败。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 原账户和数据库
    """
    base = str(deployment_client.base_url).rstrip("/")
    original = (await deployment_client.get(base)).json()["data"]
    result = await deployment_client.post("lease", json={"lease_attempt": 1})
    assert result.status_code == 200, result.text
    lease = result.json()["data"]
    assert result.json()["status"] == "leased" and not result.json()["committed"]
    for field in SkillDeploymentIdentity.model_fields:
        assert lease[field] == original[field]
    duration = datetime.fromisoformat(lease["lease_until"]) - datetime.fromisoformat(
        lease["server_time"]
    )
    assert 0 < duration.total_seconds() <= 300
    assert 0 < lease["renew_after_milliseconds"] < duration.total_seconds() * 1000
    async with prepared.database.begin() as session:
        await session.execute(
            update(NodeTask)
            .where(NodeTask.id == UUID(original["task_id"]))
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await deployment_client.get(base)).status_code == 409
    assert (await deployment_client.post("lease", json={"lease_attempt": 1})).status_code == 409


async def test_same_user_unrelated_content_is_not_a_deployment_member(
    deployment_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    同用户另一个暂存包不会扩展原任务文件授权范围。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    data = b"other user-owned private bytes"
    await LibraryHarness(prepared.database, tmp_path, prepared.owner).candidate(
        name="other", content=data
    )
    denied = await deployment_client.get("files/" + hashlib.sha256(data).hexdigest())
    assert denied.status_code == 404 and denied.json()["errors"][0]["code"] == "CONTENT_NOT_FOUND"


@pytest.mark.parametrize("revoke", [False, True])
async def test_copy_releases_locks_and_rechecks_authority_before_bytes(
    deployment_client: AsyncClient,
    prepared: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
    revoke: bool,
) -> None:
    """
    文件复制期间可续租；更换领取后已复制字节也不能发布，所有暂存均关闭。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 原数据库
    :param monkeypatch (pytest.MonkeyPatch): 精确复制边界
    :param revoke (bool): 是否在复制期间更换领取
    """
    data = (await deployment_client.get(str(deployment_client.base_url).rstrip("/"))).json()["data"]
    entry = next(entry for entry in data["manifest"]["entries"] if entry["kind"] == "file")
    entered, release = asyncio.Event(), asyncio.Event()
    targets: list[BinaryIO] = []
    copy = NodeDeploymentContent.copy_authorized_file

    async def wait_copy(
        self: NodeDeploymentContent, download: DeploymentFileDownload, target: BinaryIO
    ) -> None:
        """
        在真实文件校验之前允许另一个请求改变当前任务。

        :param download (DeploymentFileDownload): 原授权成员
        :param target (BinaryIO): 私有暂存
        """
        targets.append(target)
        entered.set()
        await release.wait()
        await copy(self, download, target)

    monkeypatch.setattr(NodeDeploymentContent, "copy_authorized_file", wait_copy)
    request = asyncio.create_task(deployment_client.get("files/" + entry["sha256"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        renewed = await asyncio.wait_for(
            deployment_client.post("lease", json={"lease_attempt": 1}), 3
        )
        assert renewed.status_code == 200
        if revoke:
            async with prepared.database.begin() as session:
                await session.execute(
                    update(NodeTask)
                    .where(NodeTask.id == UUID(data["task_id"]))
                    .values(retry_count=2)
                )
    finally:
        release.set()
    response = await asyncio.wait_for(request, 3)
    assert response.status_code == (409 if revoke else 200)
    assert targets and all(target.closed for target in targets)
    if revoke:
        assert response.json()["errors"][0]["code"] == "DEPLOYMENT_LEASE_CHANGED"


async def test_oversized_envelope_and_missing_credentials_are_rejected(
    deployment_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    编码封套上限在发送之前执行，匿名请求不会返回输入身份或清单。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param monkeypatch (pytest.MonkeyPatch): 极小传输预算
    """
    base = str(deployment_client.base_url).rstrip("/")
    denied = await deployment_client.get(base, headers={"Authorization": "Bearer wrong-node-token"})
    assert denied.status_code == 401
    monkeypatch.setattr(node_skill_deployment, "MAX_DEPLOYMENT_ENVELOPE_BYTES", 128)
    oversized = await deployment_client.get(base)
    assert (
        oversized.status_code == 413
        and oversized.json()["errors"][0]["code"] == "CONTENT_TOO_LARGE"
    )


@pytest.mark.parametrize("missing", [False, True])
async def test_corrupt_or_missing_object_never_returns_partial_success(
    deployment_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path, missing: bool
) -> None:
    """
    破坏实际私有存储字节，不能把错误前已复制的前缀当成功响应发送。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param prepared (RuntimeHarness): 原所有者
    :param tmp_path (Path): 独立私有卷
    :param missing (bool): 删除对象或写入同长度错误内容
    """
    data = (await deployment_client.get(str(deployment_client.base_url).rstrip("/"))).json()["data"]
    entry = next(entry for entry in data["manifest"]["entries"] if entry["kind"] == "file")
    digest = entry["sha256"]
    path = tmp_path / "objects" / str(prepared.owner) / digest[:2] / digest
    if missing:
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"x" * entry["size"])
        path.chmod(0o400)
    response = await deployment_client.get("files/" + digest)
    assert response.status_code == (409 if missing else 422)
    assert response.json()["errors"][0]["code"] == (
        "CONTENT_INCOMPLETE" if missing else "CONTENT_INVALID"
    )


async def test_another_authenticated_node_cannot_read_or_renew_original_input(
    deployment_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    其他节点的合法凭据不能凭完整原任务身份读取用户内容或续租。

    :param deployment_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原数据库
    :param tmp_path (Path): 服务配置路径
    """
    token = "another-deployment-node"
    async with prepared.database.begin() as session:
        session.add(
            Node(
                name="其他节点",
                status="healthy",
                region_code="global",
                node_token_hash=hash_token(settings(tmp_path).secret_key, token),
            )
        )
    headers = {"Authorization": "Bearer " + token}
    manifest = await deployment_client.get(
        str(deployment_client.base_url).rstrip("/"), headers=headers
    )
    renewed = await deployment_client.post("lease", headers=headers, json={"lease_attempt": 1})
    assert manifest.status_code == renewed.status_code == 404


async def test_changed_configuration_stops_manifest_file_and_renewal(
    deployment_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    真实新配置替代后旧任务立即失去全部内容和续租权限。

    :param deployment_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    base = str(deployment_client.base_url).rstrip("/")
    original = (await deployment_client.get(base)).json()["data"]
    entry = next(entry for entry in original["manifest"]["entries"] if entry["kind"] == "file")
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="notes",
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    for response in (
        await deployment_client.get(base),
        await deployment_client.get("files/" + entry["sha256"]),
        await deployment_client.post("lease", json={"lease_attempt": 1}),
    ):
        assert (
            response.status_code == 409
            and response.json()["errors"][0]["code"] == "OPERATION_SUPERSEDED"
        )
