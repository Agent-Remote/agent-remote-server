"""
通过真实节点认证检验部署撤权和排空确认协议。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from test_node_skill_deployment import deployment_client as deployment_client
from test_skill_content_service import database as database
from test_skill_deployment_dispatch import settings
from test_skill_deployment_termination import drained
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentTerminationIntent,
)
from agent_remote_server.security.tokens import hash_token


async def test_authenticated_drain_protocol_and_readonly_recovery(
    deployment_client: AsyncClient,
    prepared: RuntimeHarness,
) -> None:
    """
    原节点先保存撤权再提交排空，恢复查询不推进用户锁版本。

    :param deployment_client (AsyncClient): 真实认证 HTTP 客户端
    :param prepared (RuntimeHarness): 原用户数据库状态
    """
    lookup = await deployment_client.get("termination")
    assert lookup.status_code == 200 and lookup.json()["data"] == {"intent": None}
    response = await deployment_client.post(
        "termination", json={"lease_attempt": 1, "error_code": "TRANSFER_FAILED"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "drain_required" and response.json()["committed"]
    intent = SkillDeploymentTerminationIntent.model_validate(response.json()["data"])
    body = drained(intent).model_dump(mode="json")
    absent = await deployment_client.post("termination/result/inspect", json=body)
    assert absent.status_code == 200 and not absent.json()["data"]["accepted"]
    accepted = await deployment_client.post("termination/result", json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["committed"] and accepted.json()["data"]["accepted"]
    async with prepared.database() as session:
        usage = await session.get(SkillStorageUsage, prepared.owner)
        assert usage is not None
        version = usage.lock_version
    observed = await deployment_client.post("termination/result/inspect", json=body)
    assert observed.status_code == 200 and observed.json()["data"] == accepted.json()["data"]
    assert not observed.json()["committed"]
    assert (await deployment_client.get("termination")).json()["data"][
        "intent"
    ] == intent.model_dump(mode="json")
    assert (await deployment_client.post("termination/result", json=body)).json()[
        "data"
    ] == accepted.json()["data"]
    async with prepared.database() as session:
        usage = await session.get(SkillStorageUsage, prepared.owner)
        assert usage is not None and usage.lock_version == version


@pytest.mark.parametrize(
    "body",
    [
        {"lease_attempt": True, "error_code": "TRANSFER_FAILED"},
        {"lease_attempt": 1.5, "error_code": "TRANSFER_FAILED"},
        {"lease_attempt": 1, "error_code": "ARBITRARY_PRIVATE_MESSAGE"},
        {"lease_attempt": 1, "error_code": "TRANSFER_FAILED", "path": "/tmp/untrusted"},
        {"lease_attempt": None, "error_code": "TRANSFER_FAILED"},
    ],
)
async def test_termination_request_is_strict(
    deployment_client: AsyncClient, body: dict[str, object]
) -> None:
    """
    模糊轮次、任意错误正文和路径不能进入不可变撤权意图。

    :param deployment_client (AsyncClient): 真实认证客户端
    :param body (dict[str, object]): 无效请求体
    """
    assert (await deployment_client.post("termination", json=body)).status_code == 422
    assert (await deployment_client.get("termination")).json()["data"]["intent"] is None


async def test_foreign_valid_node_cannot_read_request_or_confirm_drain(
    deployment_client: AsyncClient,
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    有效其他节点凭据仍不能观察或结束原尝试。

    :param deployment_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原账户状态
    :param tmp_path (Path): 内容与服务配置路径
    """
    request = {"lease_attempt": 1, "error_code": "TRANSFER_FAILED"}
    response = await deployment_client.post("termination", json=request)
    intent = SkillDeploymentTerminationIntent.model_validate(response.json()["data"])
    token = "disposable-foreign-node"
    async with prepared.database.begin() as session:
        session.add(
            Node(
                id=uuid4(),
                name="其他节点",
                status="healthy",
                region_code="global",
                node_token_hash=hash_token(settings(tmp_path).secret_key, token),
            )
        )
    headers = {"Authorization": "Bearer " + token}
    assert (await deployment_client.get("termination", headers=headers)).status_code >= 400
    assert (
        await deployment_client.post("termination", headers=headers, json=request)
    ).status_code >= 400
    for suffix in ("termination/result", "termination/result/inspect"):
        assert (
            await deployment_client.post(
                suffix, headers=headers, json=drained(intent).model_dump(mode="json")
            )
        ).status_code >= 400
