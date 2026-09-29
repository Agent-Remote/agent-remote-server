"""
独立验证 Helper 能力恢复和已领取账号撤权，避免故障注入互相覆盖。
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from skill_deployment_scheduling_support import accept, compatible, poll
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_deployment_dispatch import settings
from test_skill_deployment_input_retention import retry
from test_skill_deployment_results import proposal
from test_skill_deployment_termination import drained
from test_skill_finalization import stopped as stopped
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, NodeTaskResult
from agent_remote_server.models.skill_deployment_attempts import SkillDeploymentAttempt
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.schemas.nodes import NodeTaskEnvelope
from agent_remote_server.schemas.skill_deployment_termination import (
    SkillDeploymentTerminationIntent,
    SkillDeploymentTerminationRequest,
)
from agent_remote_server.schemas.skill_results import SkillMutationData
from agent_remote_server.services.skills.deployment_results import NodeDeploymentResults
from agent_remote_server.services.skills.deployment_termination import NodeDeploymentTermination


@pytest.mark.parametrize("retried", [False, True])
async def test_accepted_deployment_survives_helper_capability_loss(
    prepared: RuntimeHarness, tmp_path: Path, retried: bool
) -> None:
    """
    原受理或可重试失败的后继等待完整能力恢复，再用相同操作和固定输入完成。

    :param prepared (RuntimeHarness): 独立受管账户
    :param tmp_path (Path): 私有内容卷
    :param retried (bool): 是否先完成真实失败协议并提交原操作重试
    """
    await compatible(prepared)
    accepted = await accept(prepared, tmp_path)
    attempt_id = accepted.data.targets[0].attempt_id
    original_binding = None
    if retried:
        tasks = await poll(prepared, tmp_path)
        task = next(t for t in tasks if t.task_type == "prepare_account_skills")
        async with prepared.database.begin() as session:
            original_binding = await session.get(SkillDeploymentTask, attempt_id)
            assert original_binding is not None
            service = NodeDeploymentTermination(session)
            intent = await service.request(
                prepared.node,
                task.id,
                original_binding.attempt_id,
                SkillDeploymentTerminationRequest(
                    lease_attempt=task.retry_count, error_code="NODE_UNAVAILABLE"
                ),
            )
            await service.confirm(
                prepared.node, task.id, original_binding.attempt_id, drained(intent)
            )
        attempt_id = await retry(prepared, original_binding)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        node.runtime_capabilities = {"backends": ["native"]}
    for _ in range(2):
        assert await poll(prepared, tmp_path) == []
        async with prepared.database() as session:
            operation = await session.get(SkillOperation, accepted.operation_id)
            attempt = await session.get(SkillDeploymentAttempt, attempt_id)
            assert operation is not None and operation.status == "preparing"
            assert attempt is not None and attempt.status == "pending"
            assert attempt.error_code is None and not attempt.retryable
            assert await session.get(SkillDeploymentTask, attempt_id) is None
    await compatible(prepared)
    tasks = await poll(prepared, tmp_path)
    task = next(t for t in tasks if t.task_type == "prepare_account_skills")
    assert task.payload["operation_id"] == str(accepted.operation_id)
    assert task.payload["attempt_id"] == str(attempt_id)
    async with prepared.database() as session:
        binding = await session.get(SkillDeploymentTask, attempt_id)
        assert binding is not None
        if original_binding is not None:
            assert binding.checkpoint_id == original_binding.checkpoint_id
            assert binding.plan_digest == original_binding.plan_digest
    success = await proposal(prepared, tmp_path, binding)
    async with prepared.database.begin() as session:
        observed = await NodeDeploymentResults(session, settings(tmp_path)).confirm(
            prepared.node, task.id, binding.attempt_id, success
        )
        assert observed.accepted
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None and operation.status == "ready"
        result = SkillMutationData.model_validate(operation.result_json)
        assert result.targets[0].attempt_id == attempt_id
        assert operation.generation == accepted.data.generation
        if original_binding is not None:
            previous = await session.get(SkillDeploymentAttempt, original_binding.attempt_id)
            assert previous is not None and previous.status == "failed"
            assert previous.error_code == "NODE_UNAVAILABLE" and previous.retryable


async def test_initially_unsupported_target_does_not_gain_implicit_authority(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    受理时就没有能力的目标仍需显式新配置，不把恢复等待扩大为自动部署。

    :param prepared (RuntimeHarness): 独立受管账户
    :param tmp_path (Path): 私有内容卷
    """
    await compatible(prepared)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        node.runtime_capabilities = {"backends": ["native"]}
    accepted = await accept(prepared, tmp_path)
    assert accepted.data.targets[0].readiness == "unsupported"
    await compatible(prepared)
    assert await poll(prepared, tmp_path) == []
    async with prepared.database() as session:
        attempt_id = accepted.data.targets[0].attempt_id
        attempt = await session.get(SkillDeploymentAttempt, attempt_id)
        assert attempt is not None and attempt.status == "unsupported"
        assert await session.get(SkillDeploymentTask, attempt_id) is None


async def test_http_disabled_account_after_poll_finishes_original_revocation(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    正常领取后单独禁用账号，模拟 Helper 排空回执验证撤权终态且不引入能力故障。

    :param user_client (AsyncClient): 原用户 HTTP 客户端
    :param stopped (RuntimeHarness): 独立受管账户
    :param tmp_path (Path): 私有内容卷
    """
    await compatible(stopped)
    accepted = await accept(stopped, tmp_path)
    headers = {"Authorization": "Bearer " + await token(stopped, stopped.owner, "node")}
    leased: list[NodeTaskEnvelope] = []
    for _ in range(3):
        response = await user_client.post("/api/v1/node-api/tasks/poll", headers=headers)
        assert response.status_code == 200, response.text
        leased.extend(
            NodeTaskEnvelope.model_validate(t)
            for t in response.json()["data"]["tasks"]
            if t["task_type"] == "prepare_account_skills"
        )
        if leased:
            break
    assert len(leased) == 1, response.text
    task = leased[0]
    attempt_id = accepted.data.targets[0].attempt_id
    task_id = task.task_record_id
    assert task_id is not None
    path = f"/api/v1/node/skill-deployments/{attempt_id}"
    params = {"task_id": str(task_id), "lease_attempt": str(task.lease_attempt)}
    assert (await user_client.get(path, headers=headers, params=params)).status_code == 200
    async with stopped.database() as session:
        binding = await session.get(SkillDeploymentTask, attempt_id)
        assert binding is not None
    success = await proposal(stopped, tmp_path, binding)
    disabled = await user_client.post(f"/api/v1/tool-accounts/{stopped.account}/disable")
    assert disabled.status_code == 200, disabled.text
    rejected = await user_client.get(path, headers=headers, params=params)
    assert rejected.status_code == 409
    assert rejected.json()["errors"][0]["code"] == "ACCOUNT_NOT_AVAILABLE"
    params = {"task_id": str(task_id)}
    revoked = await user_client.post(
        path + "/termination",
        headers=headers,
        params=params,
        json={"lease_attempt": task.lease_attempt, "error_code": "AUTHORIZATION_DENIED"},
    )
    assert revoked.status_code == 200, revoked.text
    intent = SkillDeploymentTerminationIntent.model_validate(revoked.json()["data"])
    assert intent.outcome == "failed" and not intent.retryable
    assert intent.error_code == "AUTHORIZATION_DENIED"
    async with stopped.database() as session:
        original = await session.get(NodeTask, task_id)
        assert original is not None and original.status == "leased"
    late = await user_client.post(
        path + "/result", headers=headers, params=params, json=success.model_dump(mode="json")
    )
    assert late.status_code == 409 and late.json()["errors"][0]["code"] == "DEPLOYMENT_REVOKED"
    receipt = drained(intent).model_dump(mode="json")
    confirmed = await user_client.post(
        path + "/termination/result", headers=headers, params=params, json=receipt
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["accepted"]
    assert confirmed.json()["data"]["task_status"] == "failed"
    replay = await user_client.post(
        path + "/termination/result", headers=headers, params=params, json=receipt
    )
    assert replay.json() == confirmed.json()
    status = await user_client.get(f"/api/v1/skills/operations/{accepted.operation_id}")
    assert status.json()["status"] == "failed" and not status.json()["retryable"]
    assert status.json()["data"]["targets"][0]["error_code"] == "AUTHORIZATION_DENIED"
    assert status.json()["data"]["replacement_id"] is None
    polled = await user_client.post("/api/v1/node-api/tasks/poll", headers=headers)
    assert all(t["task_record_id"] != str(task_id) for t in polled.json()["data"]["tasks"])
    async with stopped.database() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(NodeTaskResult)
            .where(NodeTaskResult.node_task_id == task_id)
        )
        assert count == 1
