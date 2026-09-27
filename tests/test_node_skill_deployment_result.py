"""
通过真实 Node 认证验证独立部署确认和只读原回执观察。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_node_skill_deployment import deployment_client as deployment_client
from test_skill_content_service import database as database
from test_skill_deployment_dispatch import settings
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, NodeTaskResult
from agent_remote_server.models.skill_storage import SkillStorageUsage
from agent_remote_server.schemas.skill_deployment_content import (
    SkillDeploymentContent,
    SkillDeploymentIdentity,
)
from agent_remote_server.schemas.skill_deployment_result import (
    SkillDeploymentPreparation,
    SkillDeploymentPreparedResult,
)
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.security.tokens import hash_token
from agent_remote_server.services.skills.deployment_digest import deployment_input_digest


async def http_result(client: AsyncClient) -> SkillDeploymentPreparedResult:
    """
    获取生产内容接口的原输入后构造有界模拟准备回执。

    :param client (AsyncClient): 已认证原节点客户端
    :return SkillDeploymentPreparedResult: 真实保存输入的准备结果
    """
    response = await client.get(str(client.base_url).rstrip("/"))
    assert response.status_code == 200
    content = SkillDeploymentContent.model_validate(response.json()["data"])
    return SkillDeploymentPreparedResult(
        lease_attempt=1,
        preparation=SkillDeploymentPreparation(
            version=1,
            binding=SkillDeploymentIdentity.model_validate(
                content.model_dump(include=set(SkillDeploymentIdentity.model_fields))
            ),
            input_digest=deployment_input_digest(content),
            directory_epoch=content.directory_epoch,
            generation=content.plan.generation,
            helper_receipt_id=uuid4(),
        ),
    )


async def test_authenticated_confirm_and_inspection_preserve_read_only_clocks(
    deployment_client: AsyncClient, prepared: RuntimeHarness
) -> None:
    """
    完全相同的观察和回执重放不增加持久结果或修改只读计量锁版本。

    :param deployment_client (AsyncClient): 实际认证客户端
    :param prepared (RuntimeHarness): 原账户
    """
    body = (await http_result(deployment_client)).model_dump(mode="json")
    async with prepared.database() as session:
        usage = await session.get(SkillStorageUsage, prepared.owner)
        assert usage is not None
        version = usage.lock_version
    observed = await deployment_client.post("result/inspect", json=body)
    assert observed.status_code == 200 and not observed.json()["data"]["accepted"]
    assert observed.json()["status"] == "observed" and not observed.json()["committed"]
    async with prepared.database() as session:
        usage = await session.get(SkillStorageUsage, prepared.owner)
        assert usage is not None and usage.lock_version == version
    accepted = await deployment_client.post("result", json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "confirmed" and accepted.json()["committed"]
    assert accepted.json()["data"]["result"] == body and accepted.json()["data"]["accepted"]
    repeated = await deployment_client.post("result", json=body)
    assert repeated.json()["data"] == accepted.json()["data"]
    observed = await deployment_client.post("result/inspect", json=body)
    assert observed.json()["data"] == accepted.json()["data"]


@pytest.mark.parametrize("change", ["lease", "version", "unknown", "null", "zero_helper"])
async def test_strict_result_body_cannot_smuggle_other_authority(
    deployment_client: AsyncClient, change: str
) -> None:
    """
    含混数值、空字段和未声明参数不能产生成功回执。

    :param deployment_client (AsyncClient): 认证客户端
    :param change (str): 无效输入类别
    """
    body = (await http_result(deployment_client)).model_dump(mode="json")
    if change == "lease":
        body["lease_attempt"] = True
    elif change == "version":
        body["preparation"]["version"] = True
    elif change == "unknown":
        body["path"] = "/tmp/foreign"
    elif change == "zero_helper":
        body["preparation"]["helper_receipt_id"] = str(UUID(int=0))
    else:
        body["preparation"]["generation"] = None
    for action in ("result", "result/inspect"):
        assert (await deployment_client.post(action, json=body)).status_code >= 400


async def test_another_valid_node_cannot_observe_or_confirm_original_result(
    deployment_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    原始任务身份可猜测也不能替代节点令牌归属授权。

    :param deployment_client (AsyncClient): 原节点客户端
    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 测试卷
    """
    body = (await http_result(deployment_client)).model_dump(mode="json")
    async with prepared.database.begin() as session:
        session.add(
            Node(
                id=uuid4(),
                name="other-result-node",
                region_code="test",
                status="healthy",
                supported_tool_types=["claude"],
                allowed_runtime_backends=["native"],
                node_token_hash=hash_token(
                    settings(tmp_path).secret_key, "other-result-node-token"
                ),
            )
        )
    for action in ("result", "result/inspect"):
        response = await deployment_client.post(
            action, json=body, headers={"Authorization": "Bearer other-result-node-token"}
        )
        assert response.status_code >= 400
        assert response.json()["errors"][0]["code"] == "DEPLOYMENT_NOT_FOUND"


async def test_changed_configuration_cannot_accept_late_preparation(
    deployment_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    原配置实际被替代后，迟到的本地成功不能把目标变为就绪。

    :param deployment_client (AsyncClient): 原节点
    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    result = await http_result(deployment_client)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="notes",
            idempotency_key="supersede-prepared-result",
            expected_generation=await library.generation(),
        )
    )
    response = await deployment_client.post("result", json=result.model_dump(mode="json"))
    assert (
        response.status_code >= 400
        and response.json()["errors"][0]["code"] == "OPERATION_SUPERSEDED"
    )
    observed = await deployment_client.post("result/inspect", json=result.model_dump(mode="json"))
    assert observed.status_code == 200 and not observed.json()["data"]["accepted"]


@pytest.mark.parametrize("different", [False, True])
async def test_postgres_concurrent_results_preserve_one_exact_receipt(
    deployment_client: AsyncClient, prepared: RuntimeHarness, different: bool
) -> None:
    """
    独立 PostgreSQL 连接竞争同一任务时只发布一个原始不可变结果。

    :param deployment_client (AsyncClient): 并发客户端
    :param prepared (RuntimeHarness): 原始账户
    :param different (bool): 第二个回执是否与原回执冲突
    """
    async with prepared.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL row locks")
    result = await http_result(deployment_client)
    second = result.model_dump(mode="json")
    if different:
        second["preparation"]["helper_receipt_id"] = str(uuid4())
    responses = await asyncio.gather(
        deployment_client.post("result", json=result.model_dump(mode="json")),
        deployment_client.post("result", json=second),
    )
    assert sum(response.status_code == 200 for response in responses) == (1 if different else 2)
    async with prepared.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NodeTaskResult)
                .where(NodeTaskResult.node_task_id == result.preparation.binding.task_id)
            )
            == 1
        )
        task = await session.get(NodeTask, result.preparation.binding.task_id)
        assert task is not None and task.status == "succeeded"
